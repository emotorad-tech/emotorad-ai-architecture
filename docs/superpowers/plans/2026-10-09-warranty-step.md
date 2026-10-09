# The Warranty Step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once a rider's issue is verified (the evidence check passes), code runs the warranty step for the chosen bike and acts on one of four cases: cover from the purchase date, read the OMS invoice, ask for an invoice, or the "register in the app soon" placeholder with a `register_warranty` button.

**Architecture:** `warranty_step.py` holds the pure parts: the case decision, the lines (English with Hindi drafts), the action, the masked bike view and the registration-intent check. The runtime gains a switch (`Runtime(warranty_step=True)`). While it is on, it does four things:

- **Hides the cover until the step.** The agent's view of each bike is masked, the context loses its bikes block, `lookup_warranty_record` answers `warranty_after_issue`, and the narrow path skips its warranty prefetch.
- **Runs the step at the end of a fault agent's turn** once the issue is verified.
- **No longer sends a rider with no frame straight to registration.**
- **Answers a registration-only request** with the placeholder, without calling the model.

The API turns the switch on with `EMOTORAD_WARRANTY_STEP=on`, which `deploy-staging.yml` sets.

**Tech Stack:** Python 3, stdlib `unittest`, the existing runtime, registry and invoice service.

**Spec:** `docs/superpowers/specs/2026-10-09-warranty-step-design.md`

## Global Constraints

- **Trigger.** For a customer whose turn was answered by a fault agent (`_FAULT_AGENTS`: battery, motor, narrow), once per bike per run. The key is `state.selected_frame or "-"`, recorded in `ConversationState.warranty_step_frames`.
  - Verified means `verdict_passed(state.evidence_verdict, state)` when `self._evidence_gated(state)`, otherwise `state.evidence_seen`.
  - Never on a reply where `check_safety_in_description(turn.text).triggered` or `carries_caution(turn.text)`.
  - Never on a reply a post-check replaced. Those return before the step's call site.
- **Cases:** `dated`, `invoice_on_file`, `needs_invoice`, `no_frame`. `case_of` returns `None` when the rider has bikes but none is chosen.
- **Lines** (English; Hindi drafts for a Devanagari reply):
  - `CHECKING_INVOICE_LINE = "I'm checking the invoice we have on file for your bike."`
  - `NEEDS_INVOICE_LINE = "To check your warranty, please send a clear photo or PDF of your purchase invoice showing the date."`
  - `WITH_SUPPORT_LINE = "Your invoice is already with our support team, who will confirm your warranty."`
  - `REGISTER_LATER_LINE = "Your bike isn't registered with us yet. You'll be able to register its warranty in the app soon."`
- **Action:** `REGISTER_ACTION = {"kind": "register_warranty", "label": "Register warranty"}`.
- **New verify-first wording** for a number with no bike, with the switch on: `"I couldn't find a bike registered on this number. Tell me what is happening with your bike and I'll help."`
- **Before the step**, the lookup tool answers `ok({"outcome": "warranty_after_issue", "note": ...})`, never an error. `Runtime._remember_coverage` ignores that answer.
- **Switch:** `EMOTORAD_WARRANTY_STEP`, exactly `on`, read in `api.py`. `/health` shows `warranty_step`: `on` or `off`. With it off, every behaviour is today's.
- Never edit `knowledge/`. Never write a regex through a shell heredoc. Never use bare `\b` or `\w` on Indic text. British English, no em dashes.
- `python3 -m unittest discover -s tests -t .` stays green (the known `tests.test_video` speech-to-text failure aside).

## Review Focus

1. **A rider with two bikes who has not chosen one:** the step must wait, never pick a bike, and never append a line. Pinned in Task 3.
2. **The model calls `lookup_warranty_record` before the step:** the answer must not reach `coverage_result`, so a cover claim is still blocked by the post-check. Pinned in Task 2.
3. **A case 4 rider (no frame) with a battery fault:** they must reach the battery agent, and the step must run for them with the key `"-"`. Pinned in Task 4.
4. **The step's own lookup:** it must not be gated by the switch's own fact (it runs with no `warranty_ready` fact). Otherwise case 1 never gets `coverage_result`. Pinned in Task 3.
5. **Case 2 when the invoice service is absent** (OCR off): the step must still append the line and must not crash. Pinned in Task 3.

Rulings taken while planning (the executor carries them):

- **The whole feature sits behind `Runtime(warranty_step=...)` and `EMOTORAD_WARRANTY_STEP`.** Hiding the cover and gating the lookup for every chat would change hundreds of existing tests that script `lookup_warranty_record`, and the repo rolls out each code-run ask behind a switch (melt ask, serial ask). Task 5 updates the spec to match.
- **The context's bikes block is removed while the cover is hidden** (`enrichment.without_bikes`, as the unlisted-bike path does), rather than reworded. The agent's facts block still lists the bikes, with model and frame only.
- **An unlisted bike** (the rider said the listed bike is not theirs) **is case `no_frame`**, because it has no record on this number.

---

### Task 1: `warranty_step.py`, the pure parts

**Files:**
- Create: `src/emotorad_ai/warranty_step.py`
- Test: `tests/test_warranty_step.py`

**Interfaces:**
- Consumes: `identity.ResolvedIdentity` (`bikes`, `method`); `ConversationState` (`selected_frame`, `unlisted_bike`); `tools.mocks.bike_ref`; `runtime`'s `writes_hindi` is not imported here (the caller passes `hindi`).
- Produces:
  - constants `DATED = "dated"`, `INVOICE_ON_FILE = "invoice_on_file"`, `NEEDS_INVOICE = "needs_invoice"`, `NO_FRAME = "no_frame"`, `NO_BIKE = "-"`, `AFTER_ISSUE = "after_issue"`, `REGISTER_ACTION`, the four lines and their `_HI` drafts, `NO_BIKES_HELP`, `AFTER_ISSUE_NOTE`
  - `case_of(resolved, state) -> Optional[str]`
  - `chosen_bike(resolved, state) -> Optional[Dict[str, Any]]`
  - `line_for(case: str, bike: Optional[Dict[str, Any]], hindi: bool) -> Optional[str]`
  - `actions_for(case: str) -> List[Dict[str, str]]`
  - `pending_view(bike: Dict[str, Any]) -> Dict[str, Any]`
  - `asks_to_register(text: str) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/test_warranty_step.py`:

```python
"""The warranty step's pure parts (spec 2026-10-09 warranty step, sections 3 and 4)."""

import unittest

from emotorad_ai import warranty_step as ws
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.contract import Identity

DATED = {"frame_number": "EMXP0001", "product_name": "X1", "coverage_status": "from_warranty_api",
         "purchase_date": "2025-03-12", "in_warranty": True, "months_remaining": 5, "warranty_end": "2026-03-11"}
ON_FILE = {"frame_number": "EMXP0002", "product_name": "X1", "coverage_status": "purchase_date_missing",
           "purchase_date": None, "invoice_on_file": True, "invoice_with_support": False}
NO_INVOICE = {"frame_number": "EMXP0003", "product_name": "X1", "coverage_status": "purchase_date_missing",
              "purchase_date": None, "invoice_on_file": False, "invoice_with_support": False}
WITH_SUPPORT = dict(NO_INVOICE, frame_number="EMXP0004", invoice_with_support=True)


def resolved(bikes, method="warranty_lookup"):
    return ResolvedIdentity(persona="customer", identity=Identity(phone="+919999999999", strength="verified"),
                            bikes=list(bikes), method=method)


def state(frame=None, unlisted=None):
    s = ConversationState(conversation_id="c1")
    s.selected_frame = frame
    s.unlisted_bike = unlisted
    return s


class CaseTests(unittest.TestCase):
    def test_each_case_for_the_chosen_bike(self):
        bikes = [DATED, ON_FILE, NO_INVOICE, WITH_SUPPORT]
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0001")), ws.DATED)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0002")), ws.INVOICE_ON_FILE)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0003")), ws.NEEDS_INVOICE)
        self.assertEqual(ws.case_of(resolved(bikes), state("EMXP0004")), ws.NEEDS_INVOICE)

    def test_no_frame_for_no_record_and_for_an_unlisted_bike(self):
        self.assertEqual(ws.case_of(resolved([], method="no_warranty_record"), state()), ws.NO_FRAME)
        self.assertEqual(ws.case_of(resolved([DATED]), state(unlisted={"frame_number": "TYPED1"})), ws.NO_FRAME)

    def test_bikes_but_none_chosen_waits(self):
        self.assertIsNone(ws.case_of(resolved([DATED, ON_FILE]), state()))
        self.assertIsNone(ws.case_of(resolved([DATED]), state("NOT-A-FRAME")))


class LineTests(unittest.TestCase):
    def test_lines_and_actions(self):
        self.assertIsNone(ws.line_for(ws.DATED, DATED, hindi=False))
        self.assertEqual(ws.line_for(ws.INVOICE_ON_FILE, ON_FILE, hindi=False), ws.CHECKING_INVOICE_LINE)
        self.assertEqual(ws.line_for(ws.NEEDS_INVOICE, NO_INVOICE, hindi=False), ws.NEEDS_INVOICE_LINE)
        self.assertEqual(ws.line_for(ws.NEEDS_INVOICE, WITH_SUPPORT, hindi=False), ws.WITH_SUPPORT_LINE)
        self.assertEqual(ws.line_for(ws.NO_FRAME, None, hindi=False), ws.REGISTER_LATER_LINE)
        self.assertEqual(ws.actions_for(ws.NO_FRAME), [{"kind": "register_warranty", "label": "Register warranty"}])
        self.assertEqual(ws.actions_for(ws.DATED), [])

    def test_hindi_drafts(self):
        for case, bike in ((ws.INVOICE_ON_FILE, ON_FILE), (ws.NEEDS_INVOICE, NO_INVOICE), (ws.NO_FRAME, None)):
            with self.subTest(case=case):
                line = ws.line_for(case, bike, hindi=True)
                self.assertTrue(any("ऀ" <= ch <= "ॿ" for ch in line), line)

    def test_no_line_holds_a_date_or_a_coverage_claim(self):
        for line in (ws.CHECKING_INVOICE_LINE, ws.NEEDS_INVOICE_LINE, ws.WITH_SUPPORT_LINE, ws.REGISTER_LATER_LINE):
            with self.subTest(line=line):
                self.assertNotIn("covered", line.lower())
                self.assertFalse(any(ch.isdigit() for ch in line))


class PendingViewTests(unittest.TestCase):
    def test_the_view_keeps_the_bike_and_drops_every_cover_field(self):
        view = ws.pending_view(dict(DATED, components=[{"component": "battery"}], invoice_on_file=True))
        self.assertEqual((view["frame_number"], view["product_name"], view["coverage_status"]),
                         ("EMXP0001", "X1", ws.AFTER_ISSUE))
        for key in ("in_warranty", "months_remaining", "warranty_end", "purchase_date", "components",
                    "invoice_on_file"):
            self.assertNotIn(key, view)


class IntentTests(unittest.TestCase):
    def test_registration_requests_in_english_hinglish_and_hindi(self):
        for text in ("I want to register my warranty", "warranty registration", "how do I register my bike",
                     "mujhe warranty register karna hai", "वारंटी रजिस्टर करनी है", "पंजीकरण करना है"):
            with self.subTest(text=text):
                self.assertTrue(ws.asks_to_register(text))

    def test_not_a_registration_request(self):
        for text in ("my battery won't charge", "the motor makes a noise", "register"):
            with self.subTest(text=text):
                self.assertEqual(ws.asks_to_register(text), text == "register")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_warranty_step -v`
Expected: FAIL with `ImportError: cannot import name 'warranty_step'`.

- [ ] **Step 3: Write `src/emotorad_ai/warranty_step.py`**

```python
"""The warranty step, run by code once the issue is verified (spec
2026-10-09 warranty step).

Four cases for the chosen bike: a purchase date (cover is computed), no date
with an invoice on file (code reads it), no date and no invoice (ask for
one), and no frame on record (registration comes in the app; a placeholder
line and a register_warranty button for now). Until the step has run for a
bike, the agent sees the bike without its cover (pending_view).

The Hindi lines are drafts for a Hindi speaker to check.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .tools.mocks import bike_ref

DATED = "dated"
INVOICE_ON_FILE = "invoice_on_file"
NEEDS_INVOICE = "needs_invoice"
NO_FRAME = "no_frame"
NO_BIKE = "-"
AFTER_ISSUE = "after_issue"

CHECKING_INVOICE_LINE = "I'm checking the invoice we have on file for your bike."
CHECKING_INVOICE_LINE_HI = "मैं आपकी बाइक के लिए हमारे पास मौजूद इनवॉइस देख रहा हूँ।"  # DRAFT
NEEDS_INVOICE_LINE = ("To check your warranty, please send a clear photo or PDF of your purchase invoice "
                      "showing the date.")
NEEDS_INVOICE_LINE_HI = ("वारंटी जाँचने के लिए, कृपया अपने खरीद इनवॉइस की साफ़ फ़ोटो या PDF भेजें, "
                         "जिसमें तारीख दिखे।")  # DRAFT
WITH_SUPPORT_LINE = "Your invoice is already with our support team, who will confirm your warranty."
WITH_SUPPORT_LINE_HI = "आपका इनवॉइस पहले से हमारी सपोर्ट टीम के पास है, वे आपकी वारंटी की पुष्टि करेंगे।"  # DRAFT
REGISTER_LATER_LINE = ("Your bike isn't registered with us yet. You'll be able to register its warranty "
                       "in the app soon.")
REGISTER_LATER_LINE_HI = "आपकी बाइक अभी हमारे पास रजिस्टर नहीं है। जल्द ही आप ऐप में इसकी वारंटी रजिस्टर कर पाएँगे।"  # DRAFT
REGISTER_ACTION = {"kind": "register_warranty", "label": "Register warranty"}
NO_BIKES_HELP = ("I couldn't find a bike registered on this number. Tell me what is happening with your bike "
                 "and I'll help.")
AFTER_ISSUE_NOTE = ("The warranty is checked by the platform once the fault is confirmed. Do not state, "
                    "estimate or look up coverage yet; help with the issue first.")

# Fields that say anything about cover, dropped from the agent's view of a
# bike until the step has run for it.
_COVER_FIELDS = ("in_warranty", "months_remaining", "warranty_start", "warranty_end", "warranty_start_source",
                 "purchase_date", "components", "term_months", "term_source", "invoice_on_file",
                 "invoice_with_support", "remedy", "note", "coverage", "warranty_api")

# Words that ask to register, matched as substrings of the lower-cased text
# (never \b or \w: Devanagari vowel signs fall outside \w).
_REGISTER_WORDS = ("register", "registration", "रजिस्टर", "पंजीकरण")
_WARRANTY_OR_BIKE = ("warranty", "warrenty", "bike", "cycle", "वारंटी", "बाइक", "साइकिल")


def chosen_bike(resolved: Any, state: Any) -> Optional[Dict[str, Any]]:
    if not state.selected_frame:
        return None
    for bike in resolved.bikes:
        if bike_ref(bike) == state.selected_frame:
            return bike
    return None


def case_of(resolved: Any, state: Any) -> Optional[str]:
    """The chosen bike's case, or None while the rider has bikes and none is chosen."""
    if state.unlisted_bike or resolved.method == "no_warranty_record" or not resolved.bikes:
        return NO_FRAME
    bike = chosen_bike(resolved, state)
    if bike is None:
        return None
    if bike.get("coverage_status") == "purchase_date_missing":
        return INVOICE_ON_FILE if bike.get("invoice_on_file") else NEEDS_INVOICE
    return DATED


def line_for(case: str, bike: Optional[Dict[str, Any]], hindi: bool) -> Optional[str]:
    if case == INVOICE_ON_FILE:
        return CHECKING_INVOICE_LINE_HI if hindi else CHECKING_INVOICE_LINE
    if case == NEEDS_INVOICE:
        if (bike or {}).get("invoice_with_support"):
            return WITH_SUPPORT_LINE_HI if hindi else WITH_SUPPORT_LINE
        return NEEDS_INVOICE_LINE_HI if hindi else NEEDS_INVOICE_LINE
    if case == NO_FRAME:
        return REGISTER_LATER_LINE_HI if hindi else REGISTER_LATER_LINE
    return None


def actions_for(case: str) -> List[Dict[str, str]]:
    return [dict(REGISTER_ACTION)] if case == NO_FRAME else []


def pending_view(bike: Dict[str, Any]) -> Dict[str, Any]:
    view = {key: value for key, value in bike.items() if key not in _COVER_FIELDS}
    view["coverage_status"] = AFTER_ISSUE
    return view


def asks_to_register(text: str) -> bool:
    """A request to register: a registration word on its own, or with a
    warranty or bike word. Any script; substrings only."""
    said = (text or "").lower().strip()
    if not any(word in said for word in _REGISTER_WORDS):
        return False
    return said in _REGISTER_WORDS or any(word in said for word in _WARRANTY_OR_BIKE) or "करना" in said
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_warranty_step -v`
Expected: all PASS. If `ResolvedIdentity` or `Identity` needs other required arguments, construct them as the existing `tests/test_identity_graph.py` does and record a ruling.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/warranty_step.py tests/test_warranty_step.py
git commit -m "feat(warranty-step): the four cases, the lines, the button and the masked bike view"
```

---

### Task 2: Cover hidden until the step

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`ConversationState.warranty_step_frames`)
- Modify: `src/emotorad_ai/agents/battery_support.py` (`_coverage_line` branch)
- Modify: `src/emotorad_ai/tools/mocks.py` (`lookup_warranty_record` gate)
- Modify: `src/emotorad_ai/runtime.py` (`__init__` switch; `TURN_FACT_FIELDS`; `_warranty_ready`; the agent view and context in `_run`; the `facts` dict; `_remember_coverage`; the narrow prefetch)
- Test: `tests/test_warranty_step_runtime.py` (created here, extended in Tasks 3 and 4)

**Interfaces:**
- Consumes: `warranty_step.pending_view`, `AFTER_ISSUE`, `AFTER_ISSUE_NOTE`, `NO_BIKE` (Task 1).
- Produces:
  - `Runtime(..., warranty_step: bool = False)`, `self.warranty_step`
  - `Runtime._warranty_ready(state) -> bool`
  - `ConversationState.warranty_step_frames: List[str]`
  - The tool fact `warranty_ready` (optional inject)

- [ ] **Step 1: Write the failing tests**

`tests/test_warranty_step_runtime.py`:

```python
"""The warranty step through runtime.handle() (spec 2026-10-09 warranty step)."""

import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TURN_FACT_FIELDS, Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.oms_db import to_record

TODAY = date(2026, 10, 9)
PHONE = fixtures.SESSIONS["sess-ananya"]


def record(frame, bought=None, invoice=False):
    row = {"frame_number": frame, "product_name": "T-Rex Air", "purchase_date": bought,
           "invoice_image": "f-%s" % frame if invoice else None, "status": "active"}
    return to_record(row, TODAY, invoice_state=lambda file_id: "readable" if file_id else "unreadable")


DATED = record("EMXP0001", bought=date(2025, 3, 12))
ON_FILE = record("EMXP0002", invoice=True)
NO_INVOICE = record("EMXP0003")


class FakeInvoice:
    def __init__(self):
        self.reads = []

    def start(self, jobs):
        for job in jobs:
            job()

    def read_from_oms(self, conversation_id, user_key, cluster_id, phone, frame_number):
        self.reads.append(frame_number)

    def today(self):
        return TODAY


def make(responses, records=(DATED,), step=True, invoice=None, evidence_check=False, **extra):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: list(records) if records else None)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      warranty_step=step, invoice=invoice, evidence_check=evidence_check, **extra)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, frame="EMXP0001", seen=False, cid="conv-ws"):
    state = runtime.conversations.get(cid)
    state.route_to(BATTERY)
    if frame:
        state.selected_frame = frame
    if seen:
        state.evidence_seen = True
    return runtime.handle(adapter.to_message({"conversation_id": cid, "session_token": "sess-ananya", "text": text}))


class HiddenTests(unittest.TestCase):
    def test_the_state_field_is_a_turn_fact(self):
        self.assertIn("warranty_step_frames", TURN_FACT_FIELDS)

    def test_no_cover_in_the_prompt_before_the_step(self):
        runtime, adapter, llm = make([say("Let's check the charger first.")])
        send(runtime, adapter, "my battery won't charge")
        system = llm.requests[0]["system"]
        self.assertIn("EMXP0001", system)
        self.assertIn("CHECKED AFTER THE FAULT IS CONFIRMED", system)
        self.assertNotIn("in warranty", system.lower())
        self.assertNotIn("2026-03-11", system)

    def test_the_lookup_answers_after_issue_and_sets_no_coverage_result(self):
        runtime, adapter, llm = make([call_tool(LOOKUP_WARRANTY_RECORD, {}), say("Let's check the charger.")])
        send(runtime, adapter, "is my battery covered?")
        result = json.dumps(llm.requests[1]["messages"][-1], default=str)
        self.assertIn("warranty_after_issue", result)
        self.assertIsNone(runtime.conversations.get("conv-ws").coverage_result)

    def test_with_the_switch_off_everything_is_as_today(self):
        runtime, adapter, llm = make([call_tool(LOOKUP_WARRANTY_RECORD, {}), say("You're covered.")], step=False)
        send(runtime, adapter, "is my battery covered?")
        self.assertIn("from_warranty_api", json.dumps(runtime.conversations.get("conv-ws").coverage_result))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_warranty_step_runtime -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'warranty_step'`.

- [ ] **Step 3: The state field (`src/emotorad_ai/conversation.py`)**

In `ConversationState`, after `area`:

```python
    # The bikes the warranty step has run for in this run (spec 2026-10-09
    # warranty step): each one's frame reference, "-" for a rider with no
    # bike on record. Until a bike is here, its cover is hidden from the agent.
    warranty_step_frames: List[str] = field(default_factory=list)
```

- [ ] **Step 4: The coverage line (`src/emotorad_ai/agents/battery_support.py`)**

At the top of `_coverage_line`, before the `not_registered` branch:

```python
    if bike.get("coverage_status") == "after_issue":
        # The warranty step has not run for this bike yet (warranty_step.py).
        return ("  Coverage: CHECKED AFTER THE FAULT IS CONFIRMED. The platform checks the warranty once the "
                "fault is verified. Do not state, estimate or look up coverage yet; help with the issue first.")
```

- [ ] **Step 5: The tool gate (`src/emotorad_ai/tools/mocks.py`)**

On `lookup_warranty_record`'s `@registry.register(...)`, add `optional_injects=("warranty_ready",),`. Change its signature to `def lookup_warranty_record(phone: str, warranty_ready: Optional[bool] = None) -> Dict[str, Any]:`, and make its first lines:

```python
        if warranty_ready is False:
            # The warranty step has not run for the chosen bike (spec
            # 2026-10-09 warranty step). Code calls this without the fact.
            from ..warranty_step import AFTER_ISSUE_NOTE

            return ok({"outcome": "warranty_after_issue", "note": AFTER_ISSUE_NOTE})
```

- [ ] **Step 6: The runtime (`src/emotorad_ai/runtime.py`)**

1. `__init__`: add the keyword `warranty_step: bool = False,` after `store_cards: Any = None,`, and in the body:

```python
        # The warranty step after the issue is verified (warranty_step.py,
        # spec 2026-10-09). Off: today's behaviour throughout.
        self.warranty_step = warranty_step
```

2. Add `"warranty_step_frames",` to `TURN_FACT_FIELDS` with the comment `# The warranty step's bikes (spec 2026-10-09).`
3. Add `from . import warranty_step as warranty_step_module` with the other module imports.
4. Add a method near `_selected_bike`:

```python
    def _warranty_ready(self, state: ConversationState) -> bool:
        """Whether the agent may see and look up the chosen bike's cover."""
        if not self.warranty_step:
            return True
        return (state.selected_frame or warranty_step_module.NO_BIKE) in state.warranty_step_frames
```

5. In `_run`, replace:

```python
        agent_view = replace(resolved, bikes=[unlisted_as_bike(state.unlisted_bike)]) if state.unlisted_bike else resolved
        context = ((without_bikes(state.context_block or "") if state.unlisted_bike else (state.context_block or ""))
                   + unlisted_context(state.unlisted_bike))
```

with:

```python
        agent_view = replace(resolved, bikes=[unlisted_as_bike(state.unlisted_bike)]) if state.unlisted_bike else resolved
        hide_cover = not self._warranty_ready(state)
        if hide_cover:
            # The warranty step has not run for the chosen bike: no cover in
            # anything the agent is told (spec 2026-10-09 warranty step).
            agent_view = replace(agent_view, bikes=[warranty_step_module.pending_view(b) for b in agent_view.bikes])
        context = ((without_bikes(state.context_block or "") if (state.unlisted_bike or hide_cover)
                    else (state.context_block or ""))
                   + unlisted_context(state.unlisted_bike))
```

6. In the `facts={...}` dict passed to `agent.run`, add `"warranty_ready": lambda: self._warranty_ready(state),`.
7. At the top of `_remember_coverage`, after the docstring:

```python
        if name == LOOKUP_WARRANTY_RECORD and (envelope.get("data") or {}).get("outcome") == "warranty_after_issue":
            return  # the step has not run: nothing about cover was answered
```

8. In the narrow path's prefetch loop (`for call in chosen.prefetch:`), after `if call.tool not in self.registry.specs: continue`, add:

```python
            if call.tool == LOOKUP_WARRANTY_RECORD and not self._warranty_ready(state):
                continue  # cover waits for the warranty step (spec 2026-10-09)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_warranty_step_runtime tests.test_warranty_step -v`
Expected: all PASS.

- [ ] **Step 8: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -4`
Expected: `OK`, or only the known `tests.test_video` speech-to-text failure. The switch is off by default, so no existing test changes.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/tools/mocks.py src/emotorad_ai/runtime.py tests/test_warranty_step_runtime.py
git commit -m "feat(warranty-step): cover hidden from the agent and the lookup held until the step"
```

---

### Task 3: The step

**Files:**
- Modify: `src/emotorad_ai/runtime.py` (`_with_warranty_step`, `_step_lookup`, the call before `_with_invoice_result`)
- Test: `tests/test_warranty_step_runtime.py` (add `StepTests`)

**Interfaces:**
- Consumes: Task 1's `case_of`, `chosen_bike`, `line_for`, `actions_for`, `NO_BIKE`, `INVOICE_ON_FILE`, `NO_FRAME`; Task 2's `_warranty_ready`, `warranty_step_frames`; the existing `self.invoice.start(jobs)` and `self.invoice.read_from_oms(conversation_id, user_key, cluster_id, phone, frame_number)`; `_remember_coverage(state, name, arguments, envelope)`; `verdict_passed`; `self._evidence_gated(state)`; `carries_caution`; `check_safety_in_description`; `writes_hindi`; `_FAULT_AGENTS`.
- Produces: `Runtime._with_warranty_step(message, resolved, state, turn) -> turn`, `Runtime._issue_verified(state) -> bool`, and the event `warranty_step` with fields `case` and `frame`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_warranty_step_runtime.py`, before the `if __name__` block:

```python
class StepTests(unittest.TestCase):
    def test_nothing_happens_before_the_issue_is_verified(self):
        runtime, adapter, _ = make([say("Let's check the charger.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])
        self.assertEqual(reply.actions, [])

    def test_dated_sets_the_coverage_result_and_appends_nothing(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault in your video.")])
        reply = send(runtime, adapter, "here is the video", seen=True)
        state = runtime.conversations.get("conv-ws")
        self.assertEqual(state.warranty_step_frames, ["EMXP0001"])
        self.assertIn("from_warranty_api", json.dumps(state.coverage_result))
        self.assertIn("Thanks, I can see the fault", reply.text)
        self.assertNotIn("invoice", reply.text.lower())

    def test_invoice_on_file_starts_the_read_and_says_so(self):
        invoice = FakeInvoice()
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(ON_FILE,), invoice=invoice)
        reply = send(runtime, adapter, "here is the video", frame="EMXP0002", seen=True)
        self.assertEqual(invoice.reads, ["EMXP0002"])
        self.assertIn("I'm checking the invoice we have on file for your bike.", reply.text)

    def test_invoice_on_file_without_an_invoice_service_still_says_so(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(ON_FILE,), invoice=None)
        reply = send(runtime, adapter, "here is the video", frame="EMXP0002", seen=True)
        self.assertIn("I'm checking the invoice", reply.text)

    def test_no_invoice_asks_for_one(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(NO_INVOICE,))
        reply = send(runtime, adapter, "here is the video", frame="EMXP0003", seen=True)
        self.assertIn("please send a clear photo or PDF of your purchase invoice", reply.text)

    def test_once_per_bike(self):
        runtime, adapter, _ = make([say("Thanks."), say("Anything else?")], records=(NO_INVOICE,))
        send(runtime, adapter, "here is the video", frame="EMXP0003", seen=True)
        again = send(runtime, adapter, "ok", frame="EMXP0003", seen=True)
        self.assertNotIn("purchase invoice", again.text)

    def test_a_rider_with_two_bikes_and_none_chosen_waits(self):
        runtime, adapter, _ = make([say("Which bike is it?")], records=(DATED, NO_INVOICE))
        reply = send(runtime, adapter, "here is the video", frame=None, seen=True)
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])
        self.assertNotIn("invoice", reply.text.lower())

    def test_a_hazard_reply_never_runs_the_step(self):
        runtime, adapter, _ = make([say("Please stop using the battery, it could catch fire.")],
                                   records=(NO_INVOICE,))
        send(runtime, adapter, "it smells odd", frame="EMXP0003", seen=True)
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])

    def test_with_the_evidence_check_on_only_a_passing_verdict_counts(self):
        runtime, _, _ = make([], records=(NO_INVOICE,), evidence_check=True)
        runtime._evidence_gated = lambda state: True  # a fault chat with the check on
        state = runtime.conversations.get("conv-ws")
        state.evidence_seen = True
        self.assertFalse(runtime._issue_verified(state))
        state.evidence_verdict = {"passed": True}
        self.assertTrue(runtime._issue_verified(state))

    def test_with_the_evidence_check_off_a_photo_or_video_counts(self):
        runtime, _, _ = make([], records=(NO_INVOICE,))
        state = runtime.conversations.get("conv-ws")
        self.assertFalse(runtime._issue_verified(state))
        state.evidence_seen = True
        self.assertTrue(runtime._issue_verified(state))

    def test_the_lookup_is_open_after_the_step(self):
        runtime, adapter, llm = make([say("Thanks."), call_tool(LOOKUP_WARRANTY_RECORD, {}), say("Covered.")])
        send(runtime, adapter, "here is the video", seen=True)
        send(runtime, adapter, "am I covered?")
        self.assertNotIn("warranty_after_issue", json.dumps(llm.requests[-1]["messages"][-1], default=str))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_warranty_step_runtime -v`
Expected: the `StepTests` FAIL, because `warranty_step_frames` stays empty and nothing is appended.

- [ ] **Step 3: Implement in `src/emotorad_ai/runtime.py`**

Add the methods near `_with_serial_ask`:

```python
    def _with_warranty_step(self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState,
                            turn: Any) -> Any:
        """The warranty step (spec 2026-10-09): once the issue is verified,
        once per bike, for a customer's fault agent, never on a hazard."""
        if (not self.warranty_step or resolved.persona != "customer" or turn.agent not in _FAULT_AGENTS
                or check_safety_in_description(turn.text).triggered or carries_caution(turn.text)):
            return turn
        key = state.selected_frame or warranty_step_module.NO_BIKE
        if not self._issue_verified(state) or key in state.warranty_step_frames:
            return turn
        case = warranty_step_module.case_of(resolved, state)
        if case is None:
            return turn  # bikes, none chosen yet: the step waits
        state.warranty_step_frames.append(key)
        if case != warranty_step_module.NO_FRAME:
            self._step_lookup(message, resolved, state)
        if case == warranty_step_module.INVOICE_ON_FILE and self.invoice is not None and state.selected_frame:
            frame = state.selected_frame
            self.invoice.start([lambda: self.invoice.read_from_oms(
                message.conversation_id, state.user_key, resolved.cluster_id, resolved.identity.phone, frame)])
        self.log.emit("warranty_step", message.conversation_id, case=case,
                      frame=key if key == warranty_step_module.NO_BIKE else "chosen")
        line = warranty_step_module.line_for(case, warranty_step_module.chosen_bike(resolved, state),
                                             writes_hindi(turn.text))
        actions = warranty_step_module.actions_for(case)
        if line is None and not actions:
            return turn
        text = turn.text.rstrip() + ("\n\n" + line if line else "")
        replace_turn_text(state.history, text)
        return replace(turn, text=text, actions=list(turn.actions) + [a for a in actions if a not in turn.actions])

    def _issue_verified(self, state: ConversationState) -> bool:
        """The issue counts as verified: the evidence check passed (with the
        check on for this fault chat), otherwise a photo or video reached the
        agent."""
        if self._evidence_gated(state):
            return verdict_passed(state.evidence_verdict, state)
        return bool(state.evidence_seen)

    def _step_lookup(self, message: InboundMessage, resolved: ResolvedIdentity, state: ConversationState) -> None:
        """The step's own lookup, with no warranty_ready fact so it is never
        held back; its answer becomes the chat's coverage result."""
        context = ToolContext(conversation_id=message.conversation_id, phone=resolved.identity.phone,
                              cluster_id=resolved.cluster_id, persona=resolved.persona, started_at=state.started_at)
        envelope = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, context)
        self.log.tool_call(message.conversation_id, LOOKUP_WARRANTY_RECORD, {}, envelope)
        self._remember_coverage(state, LOOKUP_WARRANTY_RECORD, {}, envelope)
```

Then, directly before `turn = self._with_invoice_result(message, resolved, state, turn)` at the end of `_run`, add:

```python
        turn = self._with_warranty_step(message, resolved, state, turn)
```

(`ToolContext`, `LOOKUP_WARRANTY_RECORD`, `replace`, `replace_turn_text`, `verdict_passed`, `carries_caution`, `check_safety_in_description` and `writes_hindi` are already imported in `runtime.py`. Check each with `grep -n` and import any that is not.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_warranty_step_runtime -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/runtime.py tests/test_warranty_step_runtime.py
git commit -m "feat(warranty-step): the step runs once the issue is verified, in four cases"
```

---

### Task 4: Case 4: no frame, routing, the placeholder and the verify-first wording

**Files:**
- Modify: `src/emotorad_ai/runtime.py` (`_node_persona` rule 3; `_finish(actions=...)`; the `VerifyFirst(...)` construction)
- Modify: `src/emotorad_ai/verify_first.py` (`VerifyFirst(no_bikes_text=NO_BIKES)`)
- Test: `tests/test_warranty_step_runtime.py` (add `NoFrameTests`)

**Interfaces:**
- Consumes: `warranty_step.asks_to_register`, `REGISTER_LATER_LINE`, `REGISTER_LATER_LINE_HI`, `REGISTER_ACTION`, `NO_BIKES_HELP`, `NO_BIKE` (Task 1); Task 3's step.
- Produces: `Runtime._finish(..., actions: Optional[List[Dict[str, Any]]] = None)`; `VerifyFirst(..., no_bikes_text: str = NO_BIKES)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_warranty_step_runtime.py`, before the `if __name__` block:

```python
class NoFrameTests(unittest.TestCase):
    def send_no_bike(self, runtime, adapter, text, seen=False, route=True):
        state = runtime.conversations.get("conv-nf")
        if route:
            state.route_to(BATTERY)  # as triage would for "my battery won't charge"
        if seen:
            state.evidence_seen = True
        return runtime.handle(adapter.to_message({"conversation_id": "conv-nf", "session_token": "sess-ananya",
                                                  "text": text}))

    def test_a_battery_fault_reaches_the_battery_agent_then_the_placeholder(self):
        runtime, adapter, llm = make([say("Let's check the charger. Please send a short video."),
                                      say("Thanks, I can see the fault.")], records=())
        first = self.send_no_bike(runtime, adapter, "my battery won't charge")
        self.assertEqual(first.handled_by, BATTERY)
        second = self.send_no_bike(runtime, adapter, "here is the video", seen=True)
        self.assertIn("You'll be able to register its warranty in the app soon.", second.text)
        self.assertIn({"kind": "register_warranty", "label": "Register warranty"}, second.actions)
        self.assertEqual(runtime.conversations.get("conv-nf").warranty_step_frames, ["-"])
        self.assertIsNone(second.ticket_id)

    def test_a_registration_request_gets_the_placeholder_without_the_model(self):
        runtime, adapter, llm = make([], records=())
        reply = self.send_no_bike(runtime, adapter, "I want to register my warranty", route=False)
        self.assertIn("register its warranty in the app soon", reply.text)
        self.assertIn({"kind": "register_warranty", "label": "Register warranty"}, reply.actions)
        self.assertEqual(llm.requests, [])

    def test_with_the_switch_off_a_rider_with_no_bike_still_goes_to_registration(self):
        runtime, adapter, _ = make([say("Could you read me the frame number?")], records=(), step=False)
        reply = self.send_no_bike(runtime, adapter, "my battery won't charge")
        self.assertEqual(reply.handled_by, "late_warranty_registration")


class VerifyFirstWordingTests(unittest.TestCase):
    def test_the_no_bikes_text_is_the_help_line_when_the_step_is_on(self):
        from emotorad_ai import warranty_step
        from emotorad_ai.verify_first import NO_BIKES

        on, _, _ = make([], records=(), step=True, verify_first=True)
        off, _, _ = make([], records=(), step=False, verify_first=True)
        self.assertIsNotNone(on.verify_gate)
        self.assertEqual(on.verify_gate.no_bikes_text, warranty_step.NO_BIKES_HELP)
        self.assertEqual(off.verify_gate.no_bikes_text, NO_BIKES)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_warranty_step_runtime -v`
Expected: `NoFrameTests` FAIL, because the rider is routed to `late_warranty_registration` and the request goes to the model.

- [ ] **Step 3: `_finish` with actions (`src/emotorad_ai/runtime.py`)**

Add `actions: Optional[List[Dict[str, Any]]] = None,` to `_finish`'s parameters after `attachments`. In the `Reply(...)` it builds, add `actions=list(actions or []),`.

- [ ] **Step 4: Case 4 routing (`src/emotorad_ai/runtime.py`, `_node_persona` rule 3)**

Replace:

```python
        if resolved.method in ("no_warranty_record",) and LATE_WARRANTY in self.agents:
            state.route_to(LATE_WARRANTY)
            return {"reply": self._run_agent_or_handover(LATE_WARRANTY, message, resolved, state)}
```

with:

```python
        if resolved.method in ("no_warranty_record",) and LATE_WARRANTY in self.agents:
            if not self.warranty_step:
                state.route_to(LATE_WARRANTY)
                return {"reply": self._run_agent_or_handover(LATE_WARRANTY, message, resolved, state)}
            if warranty_step_module.asks_to_register(message.message_text or ""):
                # Registration comes in the app (spec 2026-10-09 warranty
                # step, case 4): the placeholder, by code, no model.
                if warranty_step_module.NO_BIKE not in state.warranty_step_frames:
                    state.warranty_step_frames.append(warranty_step_module.NO_BIKE)
                hindi = writes_hindi(message.message_text or "")
                text = (warranty_step_module.REGISTER_LATER_LINE_HI if hindi
                        else warranty_step_module.REGISTER_LATER_LINE)
                self.log.emit("warranty_step", message.conversation_id, case=warranty_step_module.NO_FRAME,
                              frame=warranty_step_module.NO_BIKE)
                return {"reply": self._finish(message, state, text, "warranty_step",
                                              actions=[dict(warranty_step_module.REGISTER_ACTION)])}
            # Otherwise triage, like anyone else: the issue first.
```

- [ ] **Step 5: The verify-first wording**

In `src/emotorad_ai/verify_first.py`:
- Give `VerifyFirst.__init__` the parameter `no_bikes_text: str = NO_BIKES` and set `self.no_bikes_text = no_bikes_text`.
- In `_verified`, replace `CONFIRMED + " " + NO_BIKES` with `CONFIRMED + " " + self.no_bikes_text`.

In `src/emotorad_ai/runtime.py`, in the `VerifyFirst(...)` construction (line 588), add:

```python
no_bikes_text=(warranty_step_module.NO_BIKES_HELP if warranty_step else verify_first_no_bikes)
```

Here `verify_first_no_bikes` is `NO_BIKES`, imported from `.verify_first` with the existing names (`from .verify_first import (..., NO_BIKES as verify_first_no_bikes)`). The construction runs after `self.warranty_step` is set; move the `self.warranty_step = warranty_step` line above it if it is not.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_warranty_step_runtime tests.test_verify_first tests.test_late_warranty -v`
Expected: all PASS.

If a no-frame rider does not reach the battery agent, systematic-debug triage's handling of zero bikes and record a ruling. The spec requires that they reach their fault agent.

- [ ] **Step 7: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -4`
Expected: `OK`, or only the known speech-to-text failure.

- [ ] **Step 8: Commit**

```bash
git add src/emotorad_ai/runtime.py src/emotorad_ai/verify_first.py tests/test_warranty_step_runtime.py
git commit -m "feat(warranty-step): no frame goes to triage first; the register-in-the-app placeholder and button"
```

---

### Task 5: Switch, health, contract and documents

**Files:**
- Modify: `src/emotorad_ai/api.py` (read the switch; `Runtime(warranty_step=...)`; `/health`)
- Modify: `.github/workflows/deploy-staging.yml` (`-e EMOTORAD_WARRANTY_STEP=on`)
- Modify: `tests/test_api_health.py` (pinned body)
- Modify: `docs/contracts/amiigo-support-chat.md` (the two "today only request_location" lines)
- Modify: `docs/superpowers/specs/2026-10-09-warranty-step-design.md` (the switch ruling)
- Modify: `/Users/macbookpro/amiigo-dealer-store-cards.md` (outside the repo: a "Register warranty" section)
- Modify: `CLAUDE.md`
- Test: `tests/test_api_warranty_step.py`

**Interfaces:**
- Consumes: `Runtime(warranty_step=)` (Task 2).
- Produces: `api.WARRANTY_STEP: bool`; `/health` `warranty_step`: `"on"` or `"off"`.

- [ ] **Step 1: Write the failing test**

`tests/test_api_warranty_step.py`:

```python
"""The API's warranty step switch (spec 2026-10-09 warranty step)."""

import unittest

from tests.test_api_health import fresh_api, zoho_blank


class SwitchTests(unittest.TestCase):
    def tearDown(self):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_WARRANTY_STEP": ""}, **zoho_blank()))

    def test_off_unless_exactly_on(self):
        for value, expected in (("", "off"), ("yes", "off"), ("on", "on")):
            with self.subTest(value=value):
                api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_WARRANTY_STEP": value},
                                     **zoho_blank()))
                self.assertEqual(api.health()["warranty_step"], expected)
                self.assertEqual(api.runtime.warranty_step, expected == "on")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_api_warranty_step -v`
Expected: FAIL with `KeyError: 'warranty_step'`.

- [ ] **Step 3: Wire `src/emotorad_ai/api.py`**

Near the other switches (beside `EMOTORAD_SERIAL_ASK`):

```python
# The warranty step after the issue is verified (warranty_step.py, spec
# 2026-10-09), exactly "on"; deploy-staging.yml sets it.
WARRANTY_STEP = os.environ.get("EMOTORAD_WARRANTY_STEP", "").strip() == "on"
```

In `runtime = Runtime(...)`, add `warranty_step=WARRANTY_STEP,`. In the `/health` dict, after `"weather": ...`, add `"warranty_step": "on" if WARRANTY_STEP else "off",`. In `tests/test_api_health.py`, add `"EMOTORAD_WARRANTY_STEP": ""` to `test_offline_reports_no_secret`'s env and `"warranty_step": "off"` after `"weather": "not configured"` in its expected body.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_api_warranty_step tests.test_api_health -v`
Expected: all PASS.

- [ ] **Step 5: The deploy and the documents**

1. **`.github/workflows/deploy-staging.yml`:** in the `docker run` line, after `-e EMOTORAD_INVOICE_OCR=on`, add ` -e EMOTORAD_WARRANTY_STEP=on`.
2. **`docs/contracts/amiigo-support-chat.md`:**
   - In the `actions` row (the line with "Buttons for under this reply"), change the example to: `for example {"kind": "request_location", "label": "Share my location"} or {"kind": "register_warranty", "label": "Register warranty"}`.
   - In the `actions[]` rendering row (the line starting `` | `actions[]` | A button under the live reply ``), replace "Today only `{"kind": "request_location", ...}`" with: "Two kinds today: `request_location` (as before) and `register_warranty`, which opens the app's warranty registration once it exists (until then, ignore it)."
3. **The spec,** `docs/superpowers/specs/2026-10-09-warranty-step-design.md`, gets a new section before "Rollback":

   ```markdown
   ## Switch

   The step and everything in sections 2 to 4 sit behind `EMOTORAD_WARRANTY_STEP`, exactly `on`
   (`Runtime(warranty_step=...)`), set by `deploy-staging.yml`; `/health` shows `warranty_step`.
   Off is today's behaviour throughout. Added while planning: hiding the cover and holding the
   lookup for every chat would have changed hundreds of existing tests, and each code-run ask in
   this repo is rolled out behind a switch.
   ```

   Change the Rollback line to: "Remove `-e EMOTORAD_WARRANTY_STEP=on` from `deploy-staging.yml` and redeploy."
4. **`/Users/macbookpro/amiigo-dealer-store-cards.md`:** before "## Testing on staging", add:

   ```markdown
   ## A second button: "Register warranty"

   When a rider's bike is not registered with EMotorad, the bot's reply carries the action
   `{"kind": "register_warranty", "label": "Register warranty"}`, in the same `actions` list as
   `request_location`. Draw it as a button that opens the app's warranty registration screen once
   the backend team's flow exists. Until then, ignoring this kind is correct: the bot's text already
   tells the rider registration is coming in the app.
   ```

5. **`CLAUDE.md`:** under "Rules distilled from the build log", after the "Recent weather" entry, add:

   ```markdown
   - **The warranty step** (spec 2026-10-09, `docs/superpowers/specs/2026-10-09-warranty-step-design.md`): with `EMOTORAD_WARRANTY_STEP=on` (`Runtime(warranty_step=True)`, set by `deploy-staging.yml`), the cover is checked only once the issue is verified (`verdict_passed`, or `evidence_seen` with the evidence check off), once per bike (`ConversationState.warranty_step_frames`, `"-"` for no bike), for a fault agent's reply that carries no hazard. Before it, the agent sees each bike without its cover (`warranty_step.pending_view`), the context loses its bikes block, `lookup_warranty_record` answers `warranty_after_issue` (never stored as `coverage_result`) and the narrow path skips its warranty prefetch. At the step (`Runtime._with_warranty_step`): `dated` runs the lookup in code; `invoice_on_file` also starts the OMS invoice read and says it is checking the invoice; `needs_invoice` asks for a photo or PDF of the invoice (or says it is already with support); `no_frame` says registration comes in the app soon and adds the `register_warranty` action, no ticket. A rider with no frame goes through triage to their fault agent, not straight to `late_warranty`; a registration-only request gets the placeholder from code, no model. The Hindi lines are drafts. `/health` shows `warranty_step`.
   ```

- [ ] **Step 6: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t . 2>&1 | tail -4`
Expected: `OK`, or only the known speech-to-text failure. Note the count.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/api.py .github/workflows/deploy-staging.yml tests/test_api_health.py tests/test_api_warranty_step.py docs/contracts/amiigo-support-chat.md docs/superpowers/specs/2026-10-09-warranty-step-design.md CLAUDE.md
git commit -m "feat(warranty-step): the switch, /health, the deploy, the contract and the docs"
```

(`/Users/macbookpro/amiigo-dealer-store-cards.md` is outside the repo and is not committed.)
