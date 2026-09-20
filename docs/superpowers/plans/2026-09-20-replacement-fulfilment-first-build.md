# Replacement Fulfilment, First Build: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the bot is sure a battery (or any no-technician part) needs replacing and the bike is in warranty, it confirms the address, places a mocked replacement order, and tells the customer the order id, with a configurable approval mode deciding whether that order is approved by the bot or waits for a human.

**Architecture:** A deterministic `fulfilment` module the model reaches through one new write tool, `place_replacement_order`. Code decides technician-or-not from a YAML table, resolves the item code, checks for an order already in flight, evaluates "sure" from facts the runtime already holds, and consults the approval mode. The model may only confirm the address with the customer and call the tool. Every write is mocked. A post-check blocks any reply naming an order id no tool returned.

**Tech Stack:** Python 3.9, `unittest`, PyYAML, the existing `ToolRegistry`, `ConversationState`, `Settings`.

**Spec:** `docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md`

## Global Constraints

- India only, no-technician parts only, in-warranty only. Chargeable is refused with a remedy, never improvised.
- Every write is mocked. No OMS, ERP or Razorpay call is made in this build.
- "Sure" is evaluated in code from runtime facts. The model's confidence is never read.
- Idempotency on every write. An order in flight for the same frame and part inside 48 hours is reported, not duplicated.
- The approval mode is a `Settings` field read from `EMOTORAD_AI_APPROVAL_MODE`, values `bot`, `reasonable`, `human`, default `reasonable`.
- Replacement order ids are `RO-%05d`, distinct from ticket `EM-` and dealer `SO-`.
- British English, no em dashes, in every string the customer can read.
- Run tests with `../.venv/bin/python -m unittest <module> -v` from the repo root. The machine's default `python3` lacks the dependencies.
- Commit after every task. Never push.

---

## File structure

| File | Responsibility |
|---|---|
| `knowledge/_replacement/parts.yaml` | The decision table: part, technician, ask. Business data. |
| `src/emotorad_ai/fulfilment.py` | Load the table; `ItemCodes` (mock resolver); `ReplacementOrders` (mock store with the in-flight check); `ApprovalMode`; `is_sure()`; `decide()`. No tool registration here. |
| `src/emotorad_ai/config.py` | `approval_mode` on `Settings`. |
| `src/emotorad_ai/tools/mocks.py` | `_coverage` gains `delivery_address` and `product_id`; `_bikes_on(phone)` replaces the fixtures-only lookup in `_owned_bike`; `place_replacement_order` registered when a store is supplied. |
| `src/emotorad_ai/agents/base.py` | `Agent.run` accepts `facts`, merged into `ToolContext.late`. |
| `src/emotorad_ai/runtime.py` | Passes `evidence_seen` and `coverage_result` as facts; logs the replacement decision; runs the order-id post-check. |
| `src/emotorad_ai/guardrails.py` | `check_order_claim(reply, tool_results)`. |
| `src/emotorad_ai/agents/battery_support.py` | `PLACE_REPLACEMENT_ORDER` in `TOOL_NAMES`. |
| `prompts/battery_support.md` | A replacement section naming the tool and the address step. |
| `knowledge/battery/warranty-replacement.yaml` | Names the tool in the covered branch. |
| `src/emotorad_ai/api.py` | Builds the registry with the store and item codes. |
| `tests/test_fulfilment.py` | Table, item codes, store, sure, decide. |
| `tests/test_replacement_order_tool.py` | The tool through the registry. |
| `tests/test_order_claim_postcheck.py` | The post-check. |
| `tests/test_replacement_end_to_end.py` | Krishna's flow, scripted. |

---

### Task 1: Approval mode in settings

**Files:**
- Modify: `src/emotorad_ai/config.py`
- Test: `tests/test_fulfilment.py`

**Interfaces:**
- Produces: `Settings.approval_mode: str` in `("bot", "reasonable", "human")`; `load_settings()` raises `ValueError` on anything else.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fulfilment.py
"""Replacement fulfilment: the deterministic half.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.
Everything here is code the model cannot argue with.
"""

import os
import unittest
from unittest import mock

from emotorad_ai.config import Settings, load_settings


class ApprovalModeSettingTests(unittest.TestCase):
    def test_defaults_to_reasonable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMOTORAD_AI_APPROVAL_MODE", None)
            self.assertEqual(load_settings().approval_mode, "reasonable")

    def test_reads_the_environment(self):
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "human"}):
            self.assertEqual(load_settings().approval_mode, "human")

    def test_an_unknown_mode_is_refused_at_startup(self):
        """A typo must not silently become 'the bot approves everything'."""
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "yolo"}):
            with self.assertRaises(ValueError):
                load_settings()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: FAIL, `AttributeError: 'Settings' object has no attribute 'approval_mode'`

- [ ] **Step 3: Add the setting**

In `src/emotorad_ai/config.py`, inside `Settings`, after `log_to_stdout`:

```python
    # Who approves a replacement order the bot has decided on. See
    # docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.
    #   bot        - the bot approves sure and not-sure cases alike
    #   reasonable - the bot approves sure cases; not-sure waits for a human
    #   human      - everything waits for a human
    # The business-facing names for a panel later are "Bot in love with
    # customer", "Reasonable bot", "No brain, human approval only".
    approval_mode: str = os.environ.get("EMOTORAD_AI_APPROVAL_MODE", "reasonable")
```

And replace `load_settings`:

```python
APPROVAL_MODES = ("bot", "reasonable", "human")


def load_settings() -> Settings:
    settings = Settings()
    if settings.approval_mode not in APPROVAL_MODES:
        # A typo must not silently become "the bot approves everything".
        raise ValueError(
            "EMOTORAD_AI_APPROVAL_MODE must be one of %s, not %r"
            % (", ".join(APPROVAL_MODES), settings.approval_mode)
        )
    return settings
```

- [ ] **Step 4: Run it to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: 3 tests, OK

- [ ] **Step 5: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK. (Every existing `Settings(...)` construction in tests still works because the field has a default.)

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/config.py tests/test_fulfilment.py
git commit -m "Add the replacement approval mode to settings

Three values, default reasonable, refused at startup on anything else so a
typo cannot become the bot approving everything.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The decision table

**Files:**
- Create: `knowledge/_replacement/parts.yaml`
- Create: `src/emotorad_ai/fulfilment.py`
- Test: `tests/test_fulfilment.py`

**Interfaces:**
- Produces: `PartRule(part: str, technician: bool, ask: bool)`; `load_parts_table(directory=None) -> Dict[str, PartRule]` keyed by part; `PartsTableError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fulfilment.py`, before `if __name__`:

```python
from emotorad_ai.fulfilment import PartRule, PartsTableError, load_parts_table


class PartsTableTests(unittest.TestCase):
    def test_the_shipped_table_loads(self):
        table = load_parts_table()
        self.assertIn("battery", table)
        self.assertIn("motor", table)

    def test_battery_needs_no_technician_and_is_not_asked(self):
        rule = load_parts_table()["battery"]
        self.assertEqual(rule, PartRule(part="battery", technician=False, ask=False))

    def test_display_needs_no_technician_but_the_customer_is_asked(self):
        rule = load_parts_table()["display"]
        self.assertFalse(rule.technician)
        self.assertTrue(rule.ask)

    def test_motor_needs_a_technician(self):
        self.assertTrue(load_parts_table()["motor"].technician)

    def test_every_part_the_business_named_is_present(self):
        expected = {"battery", "charger", "display", "seat", "motor", "controller", "brakes", "suspension"}
        self.assertEqual(set(load_parts_table()), expected)

    def test_a_malformed_row_fails_at_load(self):
        """The same rule as knowledge records: a bad file must not vanish
        silently into 'no such part'."""
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / "_replacement"
            d.mkdir()
            (d / "parts.yaml").write_text("battery:\n  technician: maybe\n")
            with self.assertRaises(PartsTableError):
                load_parts_table(tmp)
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: FAIL, `ImportError: cannot import name 'PartRule'`

- [ ] **Step 3: Write the table**

Create `knowledge/_replacement/parts.yaml`:

```yaml
# Which replacement parts need a technician to fit, and which the customer is
# asked about. Business data: the owner set every row on 2026-09-20. Git is the
# audit trail and a pull request is the approval.
#
#   technician: true   fitment, alignment or complicated steps. Dealer branch.
#   technician: false  the customer can fit it. Ships to their address.
#   ask: true          no technician needed, but offer self-fit or dealer and
#                      let the customer choose.
#
# A part not in this file is "not sure" by definition and is never ordered by
# the bot on its own.

battery:
  technician: false
  ask: false
charger:
  technician: false
  ask: false
display:
  technician: false
  ask: true
seat:
  technician: false
  ask: true
motor:
  technician: true
controller:
  technician: true
brakes:
  technician: true
suspension:
  technician: true
```

- [ ] **Step 4: Write the loader**

Create `src/emotorad_ai/fulfilment.py`:

```python
"""Replacement fulfilment: the half the model cannot argue with.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.

When the bot is sure a part needs replacing, this module decides whether a
technician is needed, what the item code is, whether an order is already in
flight, whether the bot is "sure" in the code-checkable sense, and whether the
configured approval mode lets the bot approve. The model reaches all of it
through one tool and may only confirm the address with the customer.

Every write here is a mock. The OMS, the ERP and Razorpay are not called.
"""

from __future__ import annotations

import itertools
import pathlib
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

PARTS_TABLE_PATH = "_replacement/parts.yaml"


class PartsTableError(Exception):
    """A malformed parts table. Raised at load, never at order time."""


@dataclass(frozen=True)
class PartRule:
    part: str
    technician: bool
    ask: bool = False


def load_parts_table(directory: Optional[Any] = None) -> Dict[str, PartRule]:
    """part -> rule, validated the way knowledge records and the media
    catalogue are: a bad file fails loudly here rather than becoming
    'no such part' in a customer's conversation."""
    import yaml

    root = pathlib.Path(directory) if directory else pathlib.Path(__file__).resolve().parents[2] / "knowledge"
    path = root / PARTS_TABLE_PATH
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError:
        raise PartsTableError("%s: not found" % path)
    if not isinstance(raw, Mapping):
        raise PartsTableError("%s: expected a mapping of part -> rule" % path)
    table: Dict[str, PartRule] = {}
    for part, rule in raw.items():
        where = "%s: %s" % (path.name, part)
        if not isinstance(rule, Mapping):
            raise PartsTableError("%s: each part must be a mapping" % where)
        technician = rule.get("technician")
        if not isinstance(technician, bool):
            raise PartsTableError("%s: technician must be true or false" % where)
        ask = rule.get("ask", False)
        if not isinstance(ask, bool):
            raise PartsTableError("%s: ask must be true or false" % where)
        table[str(part)] = PartRule(part=str(part), technician=technician, ask=ask)
    return table
```

- [ ] **Step 5: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: 9 tests, OK

- [ ] **Step 6: Commit**

```bash
git add knowledge/_replacement/parts.yaml src/emotorad_ai/fulfilment.py tests/test_fulfilment.py
git commit -m "Add the replacement parts table

Which parts need a technician and which the customer is asked about, as
business data in the knowledge tree. Loaded and validated like the media
catalogue: a malformed row fails at startup, never at order time.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Item codes and the order store

**Files:**
- Modify: `src/emotorad_ai/fulfilment.py`
- Test: `tests/test_fulfilment.py`

**Interfaces:**
- Produces: `ItemCodes.resolve(product_name: str, part: str) -> Optional[str]`; `ReplacementOrders.in_flight(frame_number, part) -> Optional[dict]`; `ReplacementOrders.create(**payload) -> dict` with `order_id` (`RO-%05d`) and `status`; `ReplacementOrders.approve(order_id) -> dict`; `IN_FLIGHT_SECONDS = 48 * 3600`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fulfilment.py`:

```python
from emotorad_ai.fulfilment import IN_FLIGHT_SECONDS, ItemCodes, ReplacementOrders


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ItemCodesTests(unittest.TestCase):
    """A mock in the shape the ERP read will have: bike model plus part in,
    item code out. The real one walks Item -> BOM -> component; this one
    walks a dictionary."""

    def test_a_known_bike_and_part_resolve(self):
        self.assertEqual(ItemCodes().resolve("EMX Plus", "battery"), "BAT-EMX-48V")

    def test_an_unknown_bike_does_not(self):
        self.assertIsNone(ItemCodes().resolve("Not A Bike", "battery"))

    def test_an_unknown_part_does_not(self):
        self.assertIsNone(ItemCodes().resolve("EMX Plus", "flux capacitor"))

    def test_the_live_product_name_shape_resolves(self):
        """Real records look like 'X1 C Red-XX01EB0007/EM01AV01C19'. The model
        name is the part before the colour and the codes."""
        self.assertEqual(ItemCodes().resolve("X1 C Red-XX01EB0007/EM01AV01C19", "battery"), "BAT-X1C-36V")


class ReplacementOrdersTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.orders = ReplacementOrders(clock=self.clock)

    def test_an_order_gets_an_id_and_a_status(self):
        order = self.orders.create(frame_number="F1", part="battery", status="pending_approval")
        self.assertRegex(order["order_id"], r"^RO-\d{5}$")
        self.assertEqual(order["status"], "pending_approval")

    def test_nothing_is_in_flight_to_begin_with(self):
        self.assertIsNone(self.orders.in_flight("F1", "battery"))

    def test_a_fresh_order_is_in_flight(self):
        created = self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertEqual(self.orders.in_flight("F1", "battery")["order_id"], created["order_id"])

    def test_a_different_part_on_the_same_frame_is_not(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertIsNone(self.orders.in_flight("F1", "charger"))

    def test_the_same_part_on_a_different_frame_is_not(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertIsNone(self.orders.in_flight("F2", "battery"))

    def test_it_ages_out_after_48_hours(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.clock.advance(IN_FLIGHT_SECONDS + 1)
        self.assertIsNone(self.orders.in_flight("F1", "battery"))

    def test_it_is_still_in_flight_just_inside(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.clock.advance(IN_FLIGHT_SECONDS - 1)
        self.assertIsNotNone(self.orders.in_flight("F1", "battery"))

    def test_approve_flips_the_status(self):
        order = self.orders.create(frame_number="F1", part="battery", status="pending_approval")
        self.assertEqual(self.orders.approve(order["order_id"])["status"], "approved")
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: FAIL, `ImportError: cannot import name 'IN_FLIGHT_SECONDS'`

- [ ] **Step 3: Implement**

Append to `src/emotorad_ai/fulfilment.py`:

```python
# How long an order or an unpaid link counts as "in flight" for the duplicate
# check. Given once by the owner on 2026-09-20 and applied to both.
IN_FLIGHT_SECONDS = 48 * 60 * 60


def _model_name(product_name: str) -> str:
    """'X1 C Red-XX01EB0007/EM01AV01C19' -> 'X1 C'.

    Live records carry the colour and two codes after the model. The fixture
    records carry the model alone. Both have to resolve.
    """
    head = product_name.split("-", 1)[0].strip()
    words = head.split()
    # Drop a trailing colour word if there is one; models are one or two tokens.
    if len(words) > 2:
        words = words[:2]
    return " ".join(words)


class ItemCodes:
    """Bike model and part -> replacement item code.

    A mock in the exact shape the ERP read will have. The real one takes the
    record's product_id to the ERP Item, its BOM, and the component's item code,
    and is 8848's to expose. Until then this dictionary is the whole world, and
    a part it cannot resolve makes the bot "not sure" by definition.
    """

    _TABLE: Dict[str, Dict[str, str]] = {
        "EMX Plus": {"battery": "BAT-EMX-48V", "charger": "CHG-EMX-2A", "display": "DSP-EMX-LCD"},
        "X1 C": {"battery": "BAT-X1C-36V", "charger": "CHG-X1-2A", "display": "DSP-X1-LED"},
        "Doodle V3": {"battery": "BAT-DDL-36V", "charger": "CHG-DDL-2A"},
    }

    def resolve(self, product_name: Optional[str], part: str) -> Optional[str]:
        if not product_name:
            return None
        return self._TABLE.get(_model_name(product_name), {}).get(part)


@dataclass
class ReplacementOrders:
    """Stands in for the OMS order the bot will place on the customer's behalf.

    In-memory, like every other store here. Its one piece of logic is the
    duplicate check: an order for the same frame and part inside the window is
    reported back rather than placed again. That is idempotency, not suspicion.
    The chat page loses its conversation on reload, requests retry, and a
    customer asking "did that go through?" tomorrow must not get two batteries.
    """

    clock: Callable[[], float] = time.monotonic
    _orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _counter: Any = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            order_id = "RO-%05d" % next(self._counter)
            order = dict(payload, order_id=order_id, placed_at=self.clock())
            order.setdefault("status", "pending_approval")
            self._orders[order_id] = order
            return dict(order)

    def approve(self, order_id: str) -> Dict[str, Any]:
        with self._lock:
            self._orders[order_id]["status"] = "approved"
            return dict(self._orders[order_id])

    def in_flight(self, frame_number: str, part: str) -> Optional[Dict[str, Any]]:
        cutoff = self.clock() - IN_FLIGHT_SECONDS
        with self._lock:
            for order in self._orders.values():
                if (
                    order.get("frame_number") == frame_number
                    and order.get("part") == part
                    and order.get("placed_at", 0) > cutoff
                    and order.get("status") in ("pending_approval", "approved")
                ):
                    return dict(order)
        return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: 21 tests, OK

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/fulfilment.py tests/test_fulfilment.py
git commit -m "Mock item codes and the replacement order store

ItemCodes is a dictionary in the exact shape the ERP read will have: model
and part in, item code out. ReplacementOrders is the OMS order the bot will
place, with the one rule that matters now: the same frame and part inside 48
hours is reported back, not placed twice.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: "Sure" and the approval decision

**Files:**
- Modify: `src/emotorad_ai/fulfilment.py`
- Test: `tests/test_fulfilment.py`

**Interfaces:**
- Produces: `is_sure(evidence_seen: bool, in_warranty: Optional[bool], item_code: Optional[str], rule: Optional[PartRule]) -> bool`; `decide(sure: bool, approval_mode: str) -> str` returning `"approved"` or `"pending_approval"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fulfilment.py`:

```python
from emotorad_ai.fulfilment import decide, is_sure

_BATTERY = PartRule(part="battery", technician=False)


class SureTests(unittest.TestCase):
    """'Sure' is four facts the runtime already holds, never the model's own
    confidence. This is the definition the approval modes stand on."""

    def test_all_four_facts_make_it_sure(self):
        self.assertTrue(is_sure(True, True, "BAT-EMX-48V", _BATTERY))

    def test_no_photo_is_not_sure(self):
        self.assertFalse(is_sure(False, True, "BAT-EMX-48V", _BATTERY))

    def test_coverage_undetermined_is_not_sure(self):
        self.assertFalse(is_sure(True, None, "BAT-EMX-48V", _BATTERY))

    def test_out_of_warranty_is_not_sure_in_this_build(self):
        """Chargeable is build two. Until then it is not a case the bot may
        approve on its own."""
        self.assertFalse(is_sure(True, False, "BAT-EMX-48V", _BATTERY))

    def test_no_item_code_is_not_sure(self):
        self.assertFalse(is_sure(True, True, None, _BATTERY))

    def test_a_part_outside_the_table_is_not_sure(self):
        self.assertFalse(is_sure(True, True, "X", None))


class DecideTests(unittest.TestCase):
    def test_bot_mode_approves_everything(self):
        self.assertEqual(decide(True, "bot"), "approved")
        self.assertEqual(decide(False, "bot"), "approved")

    def test_reasonable_mode_approves_only_sure(self):
        self.assertEqual(decide(True, "reasonable"), "approved")
        self.assertEqual(decide(False, "reasonable"), "pending_approval")

    def test_human_mode_approves_nothing(self):
        self.assertEqual(decide(True, "human"), "pending_approval")
        self.assertEqual(decide(False, "human"), "pending_approval")

    def test_an_unknown_mode_never_approves(self):
        """Settings refuses unknown modes at startup, but this function must
        fail closed on its own too."""
        self.assertEqual(decide(True, "yolo"), "pending_approval")
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: FAIL, `ImportError: cannot import name 'decide'`

- [ ] **Step 3: Implement**

Append to `src/emotorad_ai/fulfilment.py`:

```python
def is_sure(
    evidence_seen: bool,
    in_warranty: Optional[bool],
    item_code: Optional[str],
    rule: Optional[PartRule],
) -> bool:
    """Whether the bot is sure, in the only sense the approval modes accept.

    Four facts, every one of them something the runtime already holds:

    * evidence arrived in the conversation (a photo or clip);
    * coverage was looked up and is in warranty. Chargeable is not built yet,
      so out of warranty is not a case the bot may act on alone;
    * the part resolved to an item code;
    * the part is in the table.

    The spec also names "the knowledge record's flow reached its concluding
    step". The runtime does not yet record which step a flow reached, so this
    first build approximates it with evidence_seen plus the part being one the
    table knows. That is the weakest of the four and is called out in the plan.

    The model's own confidence is never consulted. That is the point.
    """
    return bool(evidence_seen) and in_warranty is True and bool(item_code) and rule is not None


def decide(sure: bool, approval_mode: str) -> str:
    """The order's status at placement, from the configured mode.

    Fails closed: a mode this function does not know approves nothing, even
    though Settings refuses unknown modes at startup.
    """
    if approval_mode == "bot":
        return "approved"
    if approval_mode == "reasonable":
        return "approved" if sure else "pending_approval"
    return "pending_approval"
```

- [ ] **Step 4: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_fulfilment -v`
Expected: 31 tests, OK

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/fulfilment.py tests/test_fulfilment.py
git commit -m "Define sure, and the approval decision

Sure is four facts the runtime holds, never the model's confidence. decide()
maps sure and the configured mode to approved or pending_approval, and fails
closed on a mode it does not know.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Address and product on the warranty result, and one lookup for all writes

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (`_coverage`, `_owned_bike`, `build_registry`)
- Modify: `src/emotorad_ai/tools/fixtures.py` (add `full_address` to each record)
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: each `bikes[]` entry from `lookup_warranty_record` carries `delivery_address: Optional[str]` and `product_id: Optional[Any]`; `_owned_bike(phone, frame_number, bikes_on)` where `bikes_on` is a closure built once in `build_registry` over `warranty_source`, used by every write that validates a frame.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools.py` (find its imports at the top; `build_registry` and `ToolContext` are already imported there):

```python
class DeliveryAddressOnTheRecordTests(unittest.TestCase):
    """The replacement order needs somewhere to go. The real warranty response
    carries full_address; the tool was dropping it."""

    def test_the_fixture_address_reaches_the_model(self):
        registry = build_registry()
        result = registry.call("lookup_warranty_record", {}, ToolContext(conversation_id="c", phone="+919876543210"))
        bike = result["data"]["bikes"][0]
        self.assertIn("delivery_address", bike)
        self.assertTrue(bike["delivery_address"])

    def test_a_live_style_record_maps_full_address(self):
        record = {
            "frame_number": "E1", "product_name": "X1 C Red-X/Y", "purchase_date": "2025-04-01",
            "full_address": "12 MG Road, Pune 411001", "product_id": 77,
        }
        registry = build_registry(warranty_source=lambda phone: [record])
        bike = registry.call("lookup_warranty_record", {}, ToolContext(conversation_id="c", phone="+911111111111"))["data"]["bikes"][0]
        self.assertEqual(bike["delivery_address"], "12 MG Road, Pune 411001")
        self.assertEqual(bike["product_id"], 77)


class WritesValidateAgainstTheLiveRecordTests(unittest.TestCase):
    """_owned_bike read fixtures.WARRANTY_RECORDS directly, so with a live
    warranty source a ticket for a real customer validated the frame against
    fixtures, found nothing, and silently dropped the frame. Every write must
    validate against the same source the lookup used."""

    def test_a_ticket_validates_the_frame_against_the_live_source(self):
        record = {"frame_number": "LIVE1", "product_name": "X1 C", "purchase_date": "2025-04-01"}
        registry = build_registry(warranty_source=lambda phone: [record])
        result = registry.call(
            "create_support_ticket",
            {"category": "battery_power", "description": "d", "severity": "high",
             "idempotency_key": "k1", "frame_number": "LIVE1"},
            ToolContext(conversation_id="c", phone="+911111111111"),
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(registry.tickets.tickets[result["data"]["ticket_id"]]["frame_number"], "LIVE1")

    def test_a_frame_the_live_source_does_not_own_is_refused(self):
        record = {"frame_number": "LIVE1", "product_name": "X1 C", "purchase_date": "2025-04-01"}
        registry = build_registry(warranty_source=lambda phone: [record])
        result = registry.call(
            "create_support_ticket",
            {"category": "battery_power", "description": "d", "severity": "high",
             "idempotency_key": "k2", "frame_number": "SOMEONE-ELSES"},
            ToolContext(conversation_id="c", phone="+911111111111"),
        )
        self.assertEqual(result["error"]["code"], "frame_number_not_owned")
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_tools -v 2>&1 | tail -20`
Expected: the four new tests FAIL (`delivery_address` missing; ticket frame is `None`; second refusal test fails because the frame is silently accepted).

- [ ] **Step 3: Add addresses to the fixtures**

In `src/emotorad_ai/tools/fixtures.py`, add a `full_address` to every record in `WARRANTY_RECORDS`. For the first record on `+919876543210` add:

```python
        "full_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006",
```

and a plausible Indian address to each other record in the same style. (Open the file; there are a handful.)

- [ ] **Step 4: Expose them on the result, and route every write through one lookup**

In `src/emotorad_ai/tools/mocks.py`, in `_coverage`, after the `"purchase_date"` line, add:

```python
        # Where a replacement ships. The real response carries full_address;
        # the fixtures now carry one too. Read back to the customer before any
        # order is placed, never assumed.
        "delivery_address": _clean(record.get("full_address")),
        # For the ERP item-code read later. Absent on fixtures, present live.
        "product_id": record.get("product_id"),
```

Replace `_owned_bike`'s first lines. Find:

```python
    records = fixtures.WARRANTY_RECORDS.get(phone) or []
    if not records:
        return None  # no record at all; the ticket is still worth raising
```

Replace with:

```python
    records = _bikes_on(phone)
    if not records:
        return None  # no record at all; the ticket is still worth raising
```

Change `_owned_bike`'s signature to take the source explicitly, so there is no module state and two registries in one process cannot cross-talk:

```python
def _owned_bike(
    phone: str,
    frame_number: Optional[str],
    bikes_on: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
```

and use it:

```python
    records = bikes_on(phone) if bikes_on else (fixtures.WARRANTY_RECORDS.get(phone) or [])
    if not records:
        return None  # no record at all; the ticket is still worth raising
```

In `build_registry`, right after `registry = ToolRegistry()`, define the one lookup every write shares:

```python
    # Every write validates a frame against the same source the lookup used.
    # Before this, _owned_bike read the fixtures directly, and with a live
    # warranty source a ticket for a real customer validated against fixtures,
    # found nothing, and silently dropped the frame number.
    def bikes_on(phone: str) -> List[Dict[str, Any]]:
        if warranty_source is not None:
            return warranty_source(phone) or []
        return fixtures.WARRANTY_RECORDS.get(phone) or []
```

Then change every existing call `_owned_bike(phone, frame_number)` inside `build_registry` (there are several: the ticket, the booking, the warranty-proof tools) to `_owned_bike(phone, frame_number, bikes_on)`. Grep for `_owned_bike(` to find them all.

- [ ] **Step 5: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_tools -v 2>&1 | tail -5`
Expected: OK

- [ ] **Step 6: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK. If a test asserts the exact key set of a `bikes[]` entry, add the two new keys to its expectation.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py src/emotorad_ai/tools/fixtures.py tests/test_tools.py
git commit -m "Carry the delivery address on the warranty result, and validate writes against it

The real response has full_address and the tool was dropping it; a
replacement needs somewhere to go. Also found: _owned_bike read the fixtures
directly, so with a live source a real customer's ticket validated the frame
against fixtures and silently dropped it. Every write now uses the same
source the lookup did.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Conversation facts reach tools

**Files:**
- Modify: `src/emotorad_ai/agents/base.py` (`Agent.run`, `_late_identity`)
- Modify: `src/emotorad_ai/runtime.py` (`_run_agent`)
- Test: `tests/test_self_service_identity.py` (extend `IdentityArrivingMidTurnTests`)

**Interfaces:**
- Produces: `Agent.run(message, resolved, history, context="", facts=None)` where `facts: Optional[Dict[str, Callable[[], Any]]]` is merged into `ToolContext.late`; the runtime passes `{"evidence_seen": ..., "coverage_result": ...}`. A tool may then declare `injects=("evidence_seen", "coverage_result")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_self_service_identity.py`, inside `IdentityArrivingMidTurnTests`:

```python
    def test_runtime_facts_are_injectable(self):
        """A tool that declares injects=('evidence_seen',) gets the
        conversation's value, resolved at call time."""
        from emotorad_ai.tools.registry import ToolRegistry, ok

        registry = ToolRegistry()

        @registry.register("peek", "test", parameters={}, injects=("evidence_seen",))
        def peek(evidence_seen):
            return ok({"seen": evidence_seen})

        context = ToolContext(conversation_id="c1", late={"evidence_seen": lambda: True})
        self.assertEqual(registry.call("peek", {}, context)["data"]["seen"], True)
```

Note: `late` already exists; this test passes today. It pins the contract the next steps rely on. Also add, in `tests/test_agent_and_runtime.py` `RuntimeTests`:

```python
    def test_the_runtime_offers_evidence_seen_to_tools(self):
        """The order tool needs to know a photo arrived. That fact lives on
        ConversationState; the runtime has to hand it to the tool context."""
        from emotorad_ai.tools.registry import ok

        runtime, adapter, llm = make_runtime([call_tool("peek", {}), say("ok")])

        @runtime.registry.register("peek", "test", parameters={}, injects=("evidence_seen",))
        def peek(evidence_seen):
            return ok({"seen": evidence_seen})

        runtime.agents[AGENT_NAME].definition = runtime.agents[AGENT_NAME].definition.__class__(
            name=AGENT_NAME,
            tool_names=tuple(runtime.agents[AGENT_NAME].definition.tool_names) + ("peek",),
            build_system_prompt=runtime.agents[AGENT_NAME].definition.build_system_prompt,
        )
        runtime.conversations.get("conv-1").route_to(AGENT_NAME)
        runtime.conversations.get("conv-1").evidence_seen = True
        send(runtime, adapter, "here")
        peeked = [c for c in llm.requests[-1]["messages"] if c["role"] == "user" and isinstance(c["content"], list)]
        self.assertIn('"seen": true', str(peeked).replace("True", "true"))
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_agent_and_runtime.RuntimeTests.test_the_runtime_offers_evidence_seen_to_tools -v`
Expected: FAIL, the tool result contains `missing_identity` for `evidence_seen`.

- [ ] **Step 3: Implement**

In `src/emotorad_ai/agents/base.py`, change `_late_identity` to accept extra facts:

```python
    def _late_facts(
        self, conversation_id: str, facts: Optional[Dict[str, Callable[[], Any]]]
    ) -> Dict[str, Callable[[], Any]]:
        """Facts a tool may need that are only settled once the turn is under
        way, or that live on the conversation rather than the identity.

        Identity: the phone a self-service surface proves mid-turn. Only
        consulted when the channel resolved nothing, so it can never redirect a
        lookup away from an identity already established upstream.

        Conversation: whatever the runtime hands over, such as whether a photo
        has arrived. A tool that declares one of these in `injects` gets the
        live value at call time.
        """
        late: Dict[str, Callable[[], Any]] = dict(facts or {})
        if self.phone_resolver is not None:
            late["phone"] = lambda: self.phone_resolver(conversation_id)
        return late
```

Change the `run` signature and the `ToolContext` construction:

```python
    def run(
        self,
        message: InboundMessage,
        resolved: ResolvedIdentity,
        history: List[Dict[str, Any]],
        context: str = "",
        facts: Optional[Dict[str, Callable[[], Any]]] = None,
    ) -> AgentTurn:
```

and

```python
            late=self._late_facts(message.conversation_id, facts),
```

Delete the old `_late_identity` method.

In `src/emotorad_ai/runtime.py`, in `_run_agent`, change the call:

```python
        turn = self.agents[agent_name].run(
            message,
            resolved,
            state.history,
            state.context_block or "",
            # Conversation facts the order tool decides on. Lambdas, because
            # evidence_seen can flip during this very turn when a photo arrives
            # with the message that triggers the order.
            facts={
                "evidence_seen": lambda: state.evidence_seen,
                "coverage_result": lambda: state.coverage_result,
            },
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_agent_and_runtime tests.test_self_service_identity -v 2>&1 | tail -5`
Expected: OK

- [ ] **Step 5: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/agents/base.py src/emotorad_ai/runtime.py tests/test_agent_and_runtime.py tests/test_self_service_identity.py
git commit -m "Let the runtime hand conversation facts to tools

The order tool has to know whether a photo arrived and what coverage was
looked up. Both live on ConversationState. They now ride in ToolContext.late
beside the late-resolved phone, and a tool declares them in injects like any
other trusted fact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The order tool

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py` (`build_registry` signature and a new registration)
- Test: `tests/test_replacement_order_tool.py`

**Interfaces:**
- Consumes: `ReplacementOrders`, `ItemCodes`, `load_parts_table`, `is_sure`, `decide` from Task 2 to 4; `_bikes_on` from Task 5; injected `evidence_seen` and `coverage_result` from Task 6.
- Produces: tool `place_replacement_order` (constant `PLACE_REPLACEMENT_ORDER = "place_replacement_order"`), parameters `frame_number`, `part`, `confirmed_address`, `idempotency_key`; injects `phone`, `conversation_id`, `evidence_seen`, `coverage_result`; `write=True`. `build_registry(..., replacement_orders=None, item_codes=None, approval_mode="reasonable")`. Registered only when `replacement_orders` is supplied. Success data: `order_id`, `status` (`approved` or `pending_approval`), `part`, `item_code`, `delivery_address`, `already_placed` (bool). Errors: `technician_required` (remedy `dealer_visit`), `part_not_identified`, `chargeable_not_supported` (remedy `human_handoff`), `coverage_undetermined` (remedy `collect_purchase_proof`), `address_required`, `frame_number_not_owned`, `frame_number_required`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replacement_order_tool.py`:

```python
"""place_replacement_order, through the registry.

The one write the fulfilment flow makes. The model may name the part, name the
frame and pass back the address it confirmed with the customer. Everything
else is decided here: technician or not, item code, in flight, sure, and the
approval mode. Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md
"""

import unittest
from datetime import date

from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext

PHONE = "+919876543210"  # fixture: one EMX Plus, in warranty on 2026-07-28
COVERED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": True,
                               "delivery_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"}]}}
NOT_COVERED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": False}]}}
UNDETERMINED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": None}]}}


def _registry(approval_mode="reasonable", orders=None):
    return build_registry(
        today=date(2026, 7, 28),
        replacement_orders=orders or ReplacementOrders(),
        item_codes=ItemCodes(),
        approval_mode=approval_mode,
    )


def _context(evidence_seen=True, coverage=COVERED):
    return ToolContext(
        conversation_id="c1", phone=PHONE,
        late={"evidence_seen": lambda: evidence_seen, "coverage_result": lambda: coverage},
    )


def _place(registry, context, **overrides):
    args = {"frame_number": "EMXP2025004417", "part": "battery",
            "confirmed_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006",
            "idempotency_key": "k-1"}
    args.update(overrides)
    return registry.call(PLACE_REPLACEMENT_ORDER, args, context)


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_store(self):
        """An agent that cannot place an order must not be told it can."""
        self.assertNotIn(PLACE_REPLACEMENT_ORDER, build_registry().specs)

    def test_present_with_a_store(self):
        self.assertIn(PLACE_REPLACEMENT_ORDER, _registry().specs)

    def test_it_is_a_write(self):
        self.assertTrue(_registry().specs[PLACE_REPLACEMENT_ORDER].write)


class HappyPathTests(unittest.TestCase):
    def test_a_sure_in_warranty_battery_is_approved_in_reasonable_mode(self):
        result = _place(_registry(), _context())
        self.assertNotIn("error", result, result)
        data = result["data"]
        self.assertRegex(data["order_id"], r"^RO-\d{5}$")
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["item_code"], "BAT-EMX-48V")
        self.assertEqual(data["delivery_address"], "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006")
        self.assertFalse(data["already_placed"])

    def test_the_order_is_recorded(self):
        orders = ReplacementOrders()
        _place(_registry(orders=orders), _context())
        self.assertIsNotNone(orders.in_flight("EMXP2025004417", "battery"))

    def test_a_new_address_the_customer_gave_is_used(self):
        result = _place(_registry(), _context(), confirmed_address="New place, Mumbai 400001")
        self.assertEqual(result["data"]["delivery_address"], "New place, Mumbai 400001")


class ApprovalModeTests(unittest.TestCase):
    def test_human_mode_leaves_it_pending(self):
        self.assertEqual(_place(_registry("human"), _context())["data"]["status"], "pending_approval")

    def test_bot_mode_approves_even_without_a_photo(self):
        self.assertEqual(_place(_registry("bot"), _context(evidence_seen=False))["data"]["status"], "approved")

    def test_reasonable_mode_holds_a_case_with_no_photo(self):
        """Not sure: no evidence. The order still exists, pending a human."""
        result = _place(_registry("reasonable"), _context(evidence_seen=False))
        self.assertNotIn("error", result)
        self.assertEqual(result["data"]["status"], "pending_approval")


class RefusalTests(unittest.TestCase):
    def test_a_technician_part_is_refused_with_the_dealer_remedy(self):
        result = _place(_registry(), _context(), part="motor")
        self.assertEqual(result["error"]["code"], "technician_required")
        self.assertEqual(result["error"]["remedy"], "dealer_visit")

    def test_an_unknown_part_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), part="flux capacitor")["error"]["code"], "part_not_identified")

    def test_out_of_warranty_is_refused_in_this_build(self):
        result = _place(_registry(), _context(coverage=NOT_COVERED))
        self.assertEqual(result["error"]["code"], "chargeable_not_supported")
        self.assertEqual(result["error"]["remedy"], "human_handoff")

    def test_undetermined_coverage_is_refused_with_the_invoice_remedy(self):
        result = _place(_registry(), _context(coverage=UNDETERMINED))
        self.assertEqual(result["error"]["code"], "coverage_undetermined")
        self.assertEqual(result["error"]["remedy"], "collect_purchase_proof")

    def test_no_coverage_lookup_at_all_is_refused(self):
        """The registry refuses before the tool runs: coverage_result is a
        required injected fact and it is None."""
        result = _place(_registry(), _context(coverage=None))
        self.assertEqual(result["error"]["code"], "missing_identity")

    def test_an_empty_address_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), confirmed_address="  ")["error"]["code"], "address_required")

    def test_a_frame_the_customer_does_not_own_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), frame_number="NOT-MINE")["error"]["code"], "frame_number_not_owned")


class InFlightTests(unittest.TestCase):
    def test_a_second_order_reports_the_first(self):
        registry = _registry()
        first = _place(registry, _context())["data"]
        second = _place(registry, _context(), idempotency_key="k-2")["data"]
        self.assertTrue(second["already_placed"])
        self.assertEqual(second["order_id"], first["order_id"])

    def test_the_same_idempotency_key_returns_the_same_envelope(self):
        registry = _registry()
        first = _place(registry, _context())
        second = _place(registry, _context())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_replacement_order_tool -v 2>&1 | tail -5`
Expected: FAIL, `ImportError: cannot import name 'PLACE_REPLACEMENT_ORDER'`

- [ ] **Step 3: Implement the tool**

In `src/emotorad_ai/tools/mocks.py`:

Add the constant near the other tool names:

```python
PLACE_REPLACEMENT_ORDER = "place_replacement_order"
```

Add to the imports at the top:

```python
from ..fulfilment import ItemCodes, ReplacementOrders, decide, is_sure, load_parts_table
```

Extend `build_registry`'s signature, after `account_finder`:

```python
    # The replacement order the bot places on the customer's behalf. Absent
    # unless a store is supplied, so an agent that cannot place one is never
    # told it can. Item codes default to the mock resolver.
    replacement_orders: Optional["ReplacementOrders"] = None,
    item_codes: Optional["ItemCodes"] = None,
    approval_mode: str = "reasonable",
```

Add the registration inside `build_registry`, immediately before `return registry`:

```python
    if replacement_orders is not None:
        parts_table = load_parts_table()
        codes = item_codes or ItemCodes()

        @registry.register(
            PLACE_REPLACEMENT_ORDER,
            "Place a replacement-part order to the customer's address, once a flow has "
            "concluded that a part needs replacing and the customer has confirmed where to "
            "send it. Pass the address exactly as they confirmed it. This decides on its own "
            "whether the part needs a technician, whether an order is already on its way, "
            "and whether it can be approved now; read the result and say what it says. It "
            "handles in-warranty only: anything chargeable is refused and goes to a person.",
            parameters={
                "frame_number": {
                    "type": "string",
                    "description": "The bike, from lookup_warranty_record. Required when the customer owns more than one.",
                },
                "part": {
                    "type": "string",
                    "enum": sorted(parts_table),
                    "description": "The part the flow concluded needs replacing.",
                },
                "confirmed_address": {
                    "type": "string",
                    "description": (
                        "The delivery address the customer confirmed in this conversation: the one from "
                        "lookup_warranty_record if they said it is still right, or the one they gave instead."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Stable key for this order, so a retry does not place it twice.",
                },
            },
            required=("part", "confirmed_address", "idempotency_key"),
            injects=("phone", "conversation_id", "evidence_seen", "coverage_result"),
            write=True,
        )
        def place_replacement_order(
            phone: str,
            conversation_id: str,
            evidence_seen: bool,
            coverage_result: Dict[str, Any],
            part: str,
            confirmed_address: str,
            idempotency_key: str,
            frame_number: Optional[str] = None,
        ) -> Dict[str, Any]:
            rule = parts_table.get(part)
            if rule is None:
                raise ToolError("part_not_identified", "%r is not a part this flow can order." % part)
            if rule.technician:
                raise ToolError(
                    "technician_required",
                    "A %s needs a technician to fit. Route the customer to a dealer rather than "
                    "shipping it to their address." % part,
                    remedy="dealer_visit",
                )
            if not (confirmed_address or "").strip():
                raise ToolError(
                    "address_required",
                    "Read the delivery address back to the customer and pass what they confirmed.",
                )

            bike = _owned_bike(phone, frame_number, bikes_on)
            if bike is None:
                raise ToolError("frame_number_required", "No bike could be resolved for this order.")
            frame = bike["frame_number"]

            # Coverage, from the lookup the runtime remembered. Chargeable is a
            # later build; refusing it here keeps the model from improvising a
            # payment it cannot take.
            covered = None
            for entry in (coverage_result.get("data") or {}).get("bikes", []):
                if entry.get("frame_number") == frame:
                    covered = entry.get("in_warranty")
            if covered is None:
                raise ToolError(
                    "coverage_undetermined",
                    "Coverage for this bike is not settled. Ask for the invoice before ordering.",
                    remedy="collect_purchase_proof",
                )
            if covered is False:
                raise ToolError(
                    "chargeable_not_supported",
                    "This bike is out of warranty, so the part is chargeable. Chargeable "
                    "replacements are handled by a person for now; hand over rather than quote.",
                    remedy="human_handoff",
                )

            existing = replacement_orders.in_flight(frame, part)
            if existing is not None:
                return ok(
                    {
                        "order_id": existing["order_id"],
                        "status": existing["status"],
                        "part": part,
                        "item_code": existing.get("item_code"),
                        "delivery_address": existing.get("delivery_address"),
                        "already_placed": True,
                    }
                )

            item_code = codes.resolve(bike.get("product_name"), part)
            sure = is_sure(evidence_seen, covered, item_code, rule)
            if item_code is None and approval_mode != "bot":
                raise ToolError(
                    "part_not_identified",
                    "No replacement item code is on file for a %s on this model. Hand over "
                    "so a person can identify it." % part,
                    remedy="human_handoff",
                )
            status = decide(sure, approval_mode)
            order = replacement_orders.create(
                frame_number=frame,
                part=part,
                item_code=item_code,
                delivery_address=confirmed_address.strip(),
                phone=phone,
                conversation_id=conversation_id,
                sure=sure,
                status=status,
            )
            return ok(
                {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "part": part,
                    "item_code": item_code,
                    "delivery_address": order["delivery_address"],
                    "already_placed": False,
                }
            )
```

Idempotency needs no code here: `ToolRegistry.call` already requires `idempotency_key` on every `write=True` tool and returns the first envelope for a repeated key, scoped to conversation id plus tool name plus key (`registry.py`, around line 185). Across conversations the key does not match, which is exactly why the in-flight check in the store exists. That is what makes `test_the_same_idempotency_key_returns_the_same_envelope` pass. Verified on 2026-09-20 while writing this plan.

- [ ] **Step 4: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_replacement_order_tool -v 2>&1 | tail -5`
Expected: 18 tests, OK

- [ ] **Step 5: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py tests/test_replacement_order_tool.py
git commit -m "Add place_replacement_order

The one write the fulfilment flow makes. The model names the part and the
frame and passes back the address it confirmed; code decides technician or
not, item code, in flight, sure, and the approval mode, and refuses anything
chargeable with a handover. Registered only when a store is supplied.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: The order-id post-check

**Files:**
- Modify: `src/emotorad_ai/guardrails.py`
- Modify: `src/emotorad_ai/runtime.py`
- Test: `tests/test_order_claim_postcheck.py`

**Interfaces:**
- Produces: `check_order_claim(reply: str, tool_results: Sequence[dict]) -> OrderCheck(blocked: bool, reason: str = "", claimed: str = "")`; constant `ORDER_BLOCKED_MESSAGE`. The runtime runs it after the coverage check and logs `guardrail_triggered` with `suppressed_text` on a block.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_order_claim_postcheck.py`:

```python
"""A reply may only name an order the tool actually placed.

Same shape as the coverage post-check and for the same reason: the tool
running proves the tool ran, not that the reply matches what it returned. A
model that invents "your order RO-00042 is on its way" has done the Air Canada
thing with a shipment instead of a refund.
"""

import unittest

from emotorad_ai.guardrails import check_order_claim

PLACED = [{"data": {"order_id": "RO-00007", "status": "approved"}}]
NOTHING: list = []


class OrderClaimTests(unittest.TestCase):
    def test_a_reply_naming_no_order_passes(self):
        self.assertFalse(check_order_claim("I have raised a ticket for you.", NOTHING).blocked)

    def test_the_order_the_tool_placed_may_be_named(self):
        self.assertFalse(check_order_claim("Done. Order RO-00007 is on its way.", PLACED).blocked)

    def test_an_order_no_tool_placed_is_blocked(self):
        check = check_order_claim("Done. Order RO-00042 is on its way.", NOTHING)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "order_claim_without_tool_result")
        self.assertEqual(check.claimed, "RO-00042")

    def test_a_different_order_from_the_one_placed_is_blocked(self):
        check = check_order_claim("Done. Order RO-00042 is on its way.", PLACED)
        self.assertTrue(check.blocked)
        self.assertEqual(check.claimed, "RO-00042")

    def test_ticket_ids_are_not_order_ids(self):
        """EM- is a ticket. The coverage of this check is RO- alone."""
        self.assertFalse(check_order_claim("Ticket EM-00001 is open.", NOTHING).blocked)

    def test_an_in_flight_report_may_name_the_existing_order(self):
        existing = [{"data": {"order_id": "RO-00003", "already_placed": True}}]
        self.assertFalse(check_order_claim("That is already on its way as RO-00003.", existing).blocked)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_order_claim_postcheck -v 2>&1 | tail -3`
Expected: FAIL, `ImportError: cannot import name 'check_order_claim'`

- [ ] **Step 3: Implement the check**

Append to `src/emotorad_ai/guardrails.py`:

```python
# Replacement order ids as place_replacement_order issues them. Tickets are
# EM- and dealer orders SO-; only RO- is a claim this check owns.
_ORDER_ID = re.compile(r"\bRO-\d{5}\b")

ORDER_BLOCKED_MESSAGE = (
    "Let me get this confirmed for you properly. I am passing this to our support team so "
    "they can check the order and come back to you."
)


@dataclass(frozen=True)
class OrderCheck:
    blocked: bool
    reason: str = ""
    claimed: str = ""


def check_order_claim(reply: str, tool_results: Sequence[dict]) -> OrderCheck:
    """Block a reply that names an order no tool in the conversation placed.

    The same control as the coverage post-check, pointed at shipments: calling
    the order tool proves it ran, not that the reply names the order it
    returned. An order id the model made up would send a customer to wait for
    a battery nobody is sending.
    """
    placed = set()
    for result in tool_results:
        data = (result or {}).get("data") or {}
        order_id = data.get("order_id")
        if isinstance(order_id, str):
            placed.add(order_id)
    for claimed in _ORDER_ID.findall(reply):
        if claimed not in placed:
            return OrderCheck(blocked=True, reason="order_claim_without_tool_result", claimed=claimed)
    return OrderCheck(blocked=False)
```

Check that `re`, `dataclass` and `Sequence` are already imported at the top of `guardrails.py`; they are used by the coverage check.

- [ ] **Step 4: Wire it into the runtime**

In `src/emotorad_ai/runtime.py`, import `ORDER_BLOCKED_MESSAGE` and `check_order_claim` from `.guardrails` beside the existing guardrail imports. In `_run_agent`, immediately after the coverage post-check's `if coverage.blocked:` block and before the evidence post-check, add:

```python
        # The third post-check, same reason as the first: the order tool ran
        # is not the reply named the order it returned. Order results from this
        # turn only; an order id is never carried forward, so a claim on a
        # later turn has to be about an order the tool reported that turn
        # (place_replacement_order reports an in-flight order rather than
        # placing a second one, so re-asking is safe).
        order = check_order_claim(turn.text, [call["result"] for call in turn.tool_calls])
        if order.blocked:
            self.log.guardrail(
                message.conversation_id, "order_post_check",
                {"reason": order.reason, "claimed": order.claimed, "suppressed_text": turn.text},
            )
            self.log.escalation(message.conversation_id, "order_claim_blocked", turn.ticket_id)
            return self._finish(
                message, state, ORDER_BLOCKED_MESSAGE, "guardrail:order_post_check",
                escalated=True, ticket_id=turn.ticket_id,
                metadata={"blocked_reason": order.reason, "suppressed_text": turn.text},
                already_in_history=True,
            )
```

- [ ] **Step 5: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_order_claim_postcheck -v 2>&1 | tail -3`
Expected: 6 tests, OK

- [ ] **Step 6: Run the whole suite**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/guardrails.py src/emotorad_ai/runtime.py tests/test_order_claim_postcheck.py
git commit -m "Block a reply that names an order no tool placed

The coverage post-check, pointed at shipments. An invented order id sends a
customer to wait for a battery nobody is sending.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Wire it into the battery agent, the prompt, the knowledge record and the API

**Files:**
- Modify: `src/emotorad_ai/agents/battery_support.py` (`TOOL_NAMES`)
- Modify: `prompts/battery_support.md` (new section 6a; tool list in section 3)
- Modify: `knowledge/battery/warranty-replacement.yaml` (covered branch names the tool)
- Modify: `knowledge/battery/melted-terminal-or-connector.yaml` (the defect step names the tool)
- Modify: `src/emotorad_ai/api.py` (`_build_registry`)
- Test: `tests/test_replacement_end_to_end.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `api.replacement_orders` (module-level `ReplacementOrders`), the tool visible to `battery_support` on `/chat`.

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_replacement_end_to_end.py`:

```python
"""Krishna's case, end to end, with a scripted model.

Melted terminal, two photos, in warranty: the customer must hear that it is a
defect, that it is covered, where it is being sent, and the order id. Not a
service centre. Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME, TOOL_NAMES
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry

_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
ADDRESS = "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"


def _runtime(responses, approval_mode="reasonable"):
    orders = ReplacementOrders()
    registry = build_registry(
        today=date(2026, 7, 28), replacement_orders=orders, item_codes=ItemCodes(), approval_mode=approval_mode,
    )
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False, approval_mode=approval_mode),
        registry=registry, llm=ScriptedClaude(responses), log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime, WebsiteChatAdapter(runtime.resolver), orders


def _send(runtime, adapter, text, photo=False):
    runtime.conversations.get("conv-1").route_to(AGENT_NAME)
    event = {"conversation_id": "conv-1", "session_token": "sess-ananya", "text": text}
    if photo:
        event["attachments"] = [{"kind": "image", "url": _JPEG}]
    return runtime.handle(adapter.to_message(event))


class ToolVisibilityTests(unittest.TestCase):
    def test_the_battery_agent_can_place_orders(self):
        self.assertIn(PLACE_REPLACEMENT_ORDER, TOOL_NAMES)


class KrishnaTests(unittest.TestCase):
    def test_a_covered_defect_with_photos_ends_in_an_order(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found your EMX Plus. Could you send a photo of the battery terminal?"),
            say("That terminal is heat damaged, which is a defect and is covered. Is this still the right address: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "conv-1-battery"}),
            say("Done. Order RO-00001 is on its way to " + ADDRESS + ". Keep the old battery off the bike."),
        ])
        _send(runtime, adapter, "my battery terminal has melted")
        _send(runtime, adapter, "here is the photo", photo=True)
        reply = _send(runtime, adapter, "yes that address is right")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        placed = orders.in_flight("EMXP2025004417", "battery")
        self.assertIsNotNone(placed)
        self.assertEqual(placed["status"], "approved")
        self.assertEqual(placed["delivery_address"], ADDRESS)

    def test_without_a_photo_the_order_waits_for_a_human(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it."),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "k"}),
            say("I have raised order RO-00001; someone will confirm it."),
        ])
        _send(runtime, adapter, "my battery is dead, just replace it")
        reply = _send(runtime, adapter, "ok")
        self.assertFalse(reply.escalated, reply.text)
        self.assertEqual(orders.in_flight("EMXP2025004417", "battery")["status"], "pending_approval")

    def test_an_invented_order_id_is_blocked(self):
        runtime, adapter, _ = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it."),
            say("Done, order RO-00099 is on its way."),
        ])
        _send(runtime, adapter, "replace my battery")
        reply = _send(runtime, adapter, "thanks")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "guardrail:order_post_check")

    def test_asking_again_tomorrow_does_not_place_a_second_order(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it. Address still right: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "a"}),
            say("Order RO-00001 placed."),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "b"}),
            say("That is already on its way as RO-00001."),
        ])
        _send(runtime, adapter, "melted terminal", photo=True)
        _send(runtime, adapter, "yes")
        reply = _send(runtime, adapter, "did that go through? send me a battery")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        self.assertEqual(len(orders._orders), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `../.venv/bin/python -m unittest tests.test_replacement_end_to_end -v 2>&1 | tail -8`
Expected: `ToolVisibilityTests` FAILS (tool not in `TOOL_NAMES`); the Krishna tests fail with the tool filtered out of the slice.

- [ ] **Step 3: Add the tool to the agent**

In `src/emotorad_ai/agents/battery_support.py`, add `PLACE_REPLACEMENT_ORDER` to the `from ..tools.mocks import (...)` list and to `TOOL_NAMES` after `BOOK_SERVICE_SLOT`.

- [ ] **Step 4: Tell the prompt**

In `prompts/battery_support.md`, section 3 (Tools), add after the `lookup_warranty_record` line:

```markdown
- `place_replacement_order(part, confirmed_address, frame_number?)` — places the replacement to the customer's address once a flow has concluded a part needs replacing and the customer has confirmed where to send it. In warranty only; it refuses anything chargeable and tells you to hand over. It decides technician-or-not, whether an order is already on its way, and whether it can be approved now. Read its result and say what it says.
```

Add a new section after section 6 (find the `## 6.` heading and insert before `## 7.`):

```markdown
## 6a. Replacing a part

Reached only when a flow has actually concluded a part needs replacing, and only for parts the customer can fit themselves. Batteries and chargers are the common case.

1. Coverage first, from `lookup_warranty_record`, remembered for the conversation. In warranty means covered and free. Out of warranty: say plainly that it is chargeable and that a person will take it from here, and hand over. Never quote a figure.
2. Read the delivery address back, word for word from `delivery_address` on the warranty record: "Is this still the right address: …?" If they give another, use theirs. Do not place anything until they have confirmed.
3. Call `place_replacement_order` with the part, the confirmed address, and the frame. Then:
   - `status: approved` — tell them the order id, the address, and that logistics will contact them with a date. Raise the ticket with the photos in the same message if you have not already.
   - `status: pending_approval` — tell them the order id, and that someone will confirm it before it ships. Still raise the ticket.
   - `already_placed: true` — that order is already on its way. Give them the id. Do not apologise for checking.
   - `technician_required` — say a technician is needed and move to the dealer flow. Do not ship it to their home.
   - `chargeable_not_supported` or `part_not_identified` — hand over, as the error says.
4. Never say "service centre" for a part the customer can fit. Never invent an order id: the only order ids you may say are the ones this tool returned.
```

- [ ] **Step 5: Tell the knowledge records**

In `knowledge/battery/warranty-replacement.yaml`, in the `COVERED, DATES KNOWN, NO PHYSICAL DAMAGE` step, change "raise the replacement ticket in the same message" to:

```
    confirm the delivery address and place it with `place_replacement_order` in the
    same message, with the ticket beside it
```

In `knowledge/battery/melted-terminal-or-connector.yaml`, in the defect step written on 2026-09-20, change "In warranty means covered, free, and the replacement ticket raised in the same message" to:

```
    In warranty means covered, free, the address confirmed and the order placed with
    `place_replacement_order` in the same message, the ticket beside it
```

- [ ] **Step 6: Wire the API**

In `src/emotorad_ai/api.py`, add the import:

```python
from .fulfilment import ItemCodes, ReplacementOrders
```

Add after `sent_media: dict = {}`:

```python
# The replacement orders the bot places. Mocked: nothing reaches the OMS from
# here yet. Module-level so "already on its way" holds across conversations.
replacement_orders = ReplacementOrders()
```

In `_build_registry`, add to both `build_registry(...)` calls:

```python
        replacement_orders=replacement_orders,
        item_codes=ItemCodes(),
        approval_mode=settings.approval_mode,
```

- [ ] **Step 7: Run to verify it passes**

Run: `../.venv/bin/python -m unittest tests.test_replacement_end_to_end -v 2>&1 | tail -5`
Expected: 5 tests, OK

- [ ] **Step 8: Run the whole suite, including the retrieval evals and the active-prompt test**

Run: `../.venv/bin/python -m unittest discover -s tests -t .`
Expected: OK. `tests/test_retrieval_evals.py` must still report 100% top-1; `tests/test_active_prompt.py` and `tests/test_knowledge_migration.py` must still pass (they validate the prompt and records you edited).

- [ ] **Step 9: Verify against the running server**

With the dev server running (`EMOTORAD_AI_MODE=anthropic`, dev codes on), drive the fixture customer through `/message` with `session_token: "sess-ananya"` and a photo, and confirm the reply names an `RO-` order and the log shows `place_replacement_order -> ok`. If the model says "service centre", the prompt section needs strengthening; do not fix that by editing the tool.

- [ ] **Step 10: Commit**

```bash
git add src/emotorad_ai/agents/battery_support.py prompts/battery_support.md knowledge/battery/warranty-replacement.yaml knowledge/battery/melted-terminal-or-connector.yaml src/emotorad_ai/api.py tests/test_replacement_end_to_end.py
git commit -m "Place the replacement order from the battery agent

Krishna's case end to end: melted terminal, photos, in warranty, address
confirmed, order placed, id given. No service centre. The prompt and both
knowledge records name the tool; the API builds the registry with the mocked
store and the configured approval mode.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** Approval modes: Tasks 1, 4, 7. Decision table: Task 2. In flight, 48 hours: Task 3, 7. Sure, four facts, evaluated at placement, never the model: Task 4, 7 (the "record reached its concluding step" fact is approximated and says so in `is_sure`'s docstring; recorded as the weakest fact). Item code: Task 3 (mock), Task 7. Address read back: Task 5, 7, 9 (prompt step 2). Chargeable refused with handover: Task 7. Ticket beside order: Task 9 (prompt, records). Order-id post-check: Task 8. Every write mocked: Tasks 3, 7, 9. Not covered by this plan, by design: payment (build 2), technician branch (build 3), real ERP and OMS (build 4).

**Placeholders.** None. Every code step carries its code. Task 5 step 3 says "a plausible Indian address to each other record" because the fixture file has several records and the plan cannot see them; the implementer opens the file.

**Type consistency.** `PartRule(part, technician, ask)` in Tasks 2, 4, 7. `ReplacementOrders.create/approve/in_flight` in Tasks 3, 7, 9. `is_sure(evidence_seen, in_warranty, item_code, rule)` and `decide(sure, approval_mode)` in Tasks 4, 7. `check_order_claim(reply, tool_results) -> OrderCheck` in Task 8. `Agent.run(..., facts=)` in Task 6, consumed by the runtime in Task 6 and relied on in Task 7's injects. `build_registry(replacement_orders=, item_codes=, approval_mode=)` in Tasks 7, 9. Settings field `approval_mode` in Tasks 1, 9.

**Verified while writing.** Task 7 relies on the registry's existing idempotency for `write=True` tools; `ToolRegistry.call` was read and it does exactly that, keyed on conversation id plus tool name plus `idempotency_key`; cross-conversation duplicates are the store's job, not the registry's.
