# Custom Bot Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let internal staff add a new sub-agent as a YAML spec from the playground and test it through the same runtime production uses (triage, mocked tools, guardrails, disclosure).

**Architecture:** A `BotCatalogue` loads the four built-in Python agents plus YAML specs (published under `bots/`, drafts under a mounted directory) into one list; runtime, triage keywords, the `search_knowledge` topic enum and the fallback reply are derived from it. A generic agent turns a spec into the same `AgentDefinition` the Python agents produce, reusing prompt blocks moved into `agents/blocks.py`. The Streamlit playground gains a "New bot" mode and its chat panel drives a real `Runtime` with an in-memory log, a `StaticResolver`, and either the `OfflinePlanner` or a new `AnthropicClaude` client.

**Tech Stack:** Python 3.12, `unittest`, PyYAML, Streamlit, `anthropic` SDK. Spec: `docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md`.

## Global Constraints

- Repo: `~/emotorad/emotorad-ai-architecture`, branch `feat/custom-bot-builder`. 4-space indentation, `from __future__ import annotations`, docstrings explain *why*.
- Tests: `python3 -m unittest discover -s tests -t .` from the repo root must stay green after every task. Single module: `python3 -m unittest tests.test_bots -v`.
- No new dependencies. `pyyaml`, `anthropic`, `streamlit` are already in `requirements.txt`.
- Built-in agent behaviour must not change: `tests/test_triage.py`, `tests/test_motor_support.py`, `tests/test_dealer_orders.py` pass unmodified.
- Custom bots select from existing tools only; persona allowlists live in code, never in a spec.
- Guardrails (`guardrails.py`), disclosure, and the coverage post-check are not touched.
- Nothing the playground does writes to tracked files. Drafts live under the drafts directory (`EMOTORAD_AI_BOT_DRAFTS`, default `.playground/drafts`), exports under `.playground/export/`.
- Commit after every task with a conventional-commit message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/emotorad_ai/agents/blocks.py` (new) | Persona-level prompt blocks shared by every sub-agent |
| `src/emotorad_ai/agents/battery_support.py`, `motor_support.py`, `dealer_orders.py` (modify) | Import blocks; declare `TOPIC`/`KEYWORDS` |
| `src/emotorad_ai/triage.py` (modify) | Keyword table and supported-topics summary become inputs |
| `src/emotorad_ai/tools/mocks.py` (modify) | `build_registry(topics=...)` drives the `search_knowledge` enum |
| `src/emotorad_ai/bots.py` (new) | `BotSpec`, YAML loading, validation, `BotCatalogue` |
| `src/emotorad_ai/agents/generic.py` (new) | `definition_from_spec` |
| `src/emotorad_ai/runtime.py` (modify) | Everything about agents derived from the catalogue; dealer keyword routing |
| `src/emotorad_ai/llm.py` (modify) | `AnthropicClaude`, shared response mapping |
| `src/emotorad_ai/identity.py` (modify) | `StaticResolver` |
| `src/emotorad_ai/agents/base.py` (modify) | Attachments become content blocks in the model history |
| `src/emotorad_ai/playground_runtime.py` (new) | `PlaygroundSession`: a real `Runtime` per playground session, with trace |
| `src/emotorad_ai/playground_bots.py` (new) | Draft/export IO, prompt templates, catalogue loading with drafts |
| `src/emotorad_ai/playground_riders.py` (new) | Rider scenarios and custom-rider forms, moved out of `playground.py` |
| `src/emotorad_ai/playground.py` (rewrite) | Streamlit UI: Chat mode and New bot mode |
| `bots/README.md` (new) | The published bot directory and spec format |
| `Dockerfile`, `.github/workflows/deploy-staging.yml`, `README.md`, `CLAUDE.md` (modify) | Ship `bots/`, mount drafts, document |

---

### Task 1: Move the shared prompt blocks to `agents/blocks.py`

**Files:**
- Create: `src/emotorad_ai/agents/blocks.py`
- Modify: `src/emotorad_ai/agents/battery_support.py:84-190`, `src/emotorad_ai/agents/motor_support.py:30`, `src/emotorad_ai/agents/dealer_orders.py:71-94`
- Test: `tests/test_blocks.py`

**Interfaces:**
- Produces: `blocks._describe(bike)`, `blocks._coverage_line(bike)`, `blocks._facts_block(resolved)`, `blocks._context_block(context)`, `blocks._entry_block(message)`, `blocks._account_block(resolved)` with the exact signatures they have today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_blocks.py
"""The prompt blocks are persona-level, shared by every sub-agent of that persona.

A second copy would drift, and the copy that drifts is the one that starts
stating coverage it should not. So there is exactly one, in agents/blocks.py.
"""

import unittest

from emotorad_ai.agents import battery_support, blocks, dealer_orders, motor_support


class SharedBlocksTests(unittest.TestCase):
    def test_every_customer_agent_uses_the_one_facts_block(self):
        for module in (battery_support, motor_support):
            self.assertIs(
                module.build_system_prompt.__globals__["_facts_block"], blocks._facts_block, module.__name__
            )
            self.assertIs(
                module.build_system_prompt.__globals__["_context_block"], blocks._context_block
            )
            self.assertIs(module.build_system_prompt.__globals__["_entry_block"], blocks._entry_block)

    def test_the_dealer_agent_uses_the_one_account_block(self):
        self.assertIs(
            dealer_orders.build_system_prompt.__globals__["_account_block"], blocks._account_block
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/emotorad/emotorad-ai-architecture && python3 -m unittest tests.test_blocks -v`
Expected: FAIL with `ImportError: cannot import name 'blocks'`

- [ ] **Step 3: Create `blocks.py` with the functions moved verbatim**

```python
# src/emotorad_ai/agents/blocks.py
"""Prompt blocks shared by every sub-agent of a persona.

These are persona-level concerns — what the platform verified about the person,
what enrichment inferred, how they arrived, what a dealer's account looks like —
not battery- or motor-specific ones. One copy, imported everywhere: a second copy
would drift, and the copy that drifts is the one that starts stating coverage it
should not. `tests/test_blocks.py` asserts the identity, not just the behaviour.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..contract import InboundMessage
from ..identity import ResolvedIdentity


def _describe(bike: Dict[str, Any]) -> str:
    parts = [bike.get("product_name") or "unknown model"]
    if bike.get("product_color"):
        parts.append("(%s)" % bike["product_color"])
    parts.append("frame %s" % bike["frame_number"])
    return " ".join(parts)


def _coverage_line(bike: Dict[str, Any]) -> str:
    """One line of coverage, or an explicit instruction not to claim any."""
    if bike.get("coverage_status") == "purchase_date_missing":
        # The one case where saying nothing is not enough — the agent has to know
        # what to *do*, or it will apologise and stop rather than ask for the
        # invoice that unblocks the whole conversation.
        return (
            "  Coverage: UNKNOWN — no purchase date on record. Do not state or estimate any "
            "coverage. Tell the customer you can see the bike but need their invoice or proof "
            "of purchase showing the date they bought it, and that someone will confirm "
            "coverage once it is checked."
        )
    if bike.get("in_warranty"):
        return "  Coverage: in warranty, about %d month(s) left of a %d month term." % (
            bike["months_remaining"],
            bike["term_months"],
        )
    return (
        "  Coverage: out of warranty (%d month term from %s). Say so plainly and kindly if it "
        "becomes relevant; any repair would be chargeable." % (bike["term_months"], bike["warranty_start"])
    )


def _facts_block(resolved: ResolvedIdentity) -> str:
    if resolved.method == "no_warranty_record":
        # A verified person with no registered bike is not a stranger — most
        # likely an owner who skipped registration. Sending them to "support"
        # is the dead end this path exists to remove.
        return (
            "\nCustomer context: this person is verified, but no bike is registered against "
            "their number. Warranty registration is often skipped, so treat them as a genuine "
            "owner. Do not state any bike model, frame number or coverage — you have none. "
            "Help with general battery questions, and offer to register their bike now so we "
            "can support it properly."
        )

    if resolved.method == "oms_error":
        # Distinguishable from the above on purpose: this is our outage, not
        # their missing record, and it is worth being honest about.
        return (
            "\nCustomer context: unavailable — our warranty system is not responding right now. "
            "Do not state any bike or coverage, and do not suggest the customer is unregistered; "
            "we simply cannot see their record. Help with general battery questions and offer to "
            "have someone follow up."
        )

    if not resolved.is_known_customer:
        return (
            "\nCustomer context: not available. You are not able to confirm ownership or "
            "warranty for this person, so do not state either. Help with general battery "
            "questions only, and offer to connect them to the support team."
        )

    name = (resolved.profile or {}).get("name") or "unknown"
    lines: List[str] = [
        "\nCustomer context (already verified — treat as fact, do not ask for it again):",
        "- Name: %s" % name,
    ]

    if len(resolved.bikes) > 1:
        # Ownership sets the option set; the customer picks from it. Guessing which
        # of three bikes they mean produces confident, wrong troubleshooting.
        lines.append(
            "- This customer owns %d bikes. Do NOT assume which one they mean. Ask them to "
            "choose before giving any bike-specific advice:" % len(resolved.bikes)
        )
    for bike in resolved.bikes:
        lines.append("- %s" % _describe(bike))
        if bike.get("battery_variant"):
            lines.append("  Battery: %s" % bike["battery_variant"])
        lines.append(_coverage_line(bike))

    return "\n".join(lines)


def _context_block(context: str) -> str:
    """What enrichment assembled — browsing, signals, past contact.

    Kept separate from the verified customer facts above it, because it is a
    different kind of knowledge: this may *personalise* a reply, but it never
    authorises a claim about what someone owns or what is covered.
    """
    if not context.strip():
        return ""
    return (
        "\n\nWhat we know about this person (may personalise your reply; never treat as "
        "proof of ownership or coverage):\n" + context.strip()
    )


def _entry_block(message: InboundMessage) -> str:
    pill = message.pill_clicked
    if not pill:
        return ""
    return (
        "\n\nThe customer arrived by tapping the '%s' option, so their intent is already known. "
        "Do not ask what the problem area is — go straight to the specific symptom." % pill
    )


def _account_block(resolved: ResolvedIdentity) -> str:
    profile = resolved.profile or {}
    if not profile:
        return (
            "\nDealer context: unavailable. Do not quote or place anything until "
            "get_dealer_account succeeds."
        )

    available = max(profile.get("credit_limit", 0) - profile.get("credit_used", 0), 0)
    lines = [
        "\nDealer context (verified — treat as fact):",
        "- Dealer: %s (%s), %s" % (profile.get("name"), profile.get("dealer_id"), profile.get("city")),
        "- Credit available: %d of %d" % (available, profile.get("credit_limit", 0)),
        "- Payment terms: %d days" % profile.get("payment_terms_days", 0),
    ]
    if profile.get("overdue_amount"):
        lines.append(
            "- OVERDUE: %d. New orders are blocked until this is cleared. Say so early rather "
            "than after quoting, so the dealer is not led on."
            % profile["overdue_amount"]
        )
    if profile.get("status") != "active":
        lines.append("- Account status: %s. Orders are blocked." % profile.get("status"))
    return "\n".join(lines)
```

- [ ] **Step 4: Point the three agents at `blocks.py`**

In `src/emotorad_ai/agents/battery_support.py`: delete the definitions of `_describe`, `_coverage_line`, `_facts_block`, `_context_block`, `_entry_block` (currently lines 84–190). Replace the `from typing import Any, Dict, List` line with nothing (no longer used) and add, after the `.base` import:

```python
from .blocks import _context_block, _entry_block, _facts_block
```

The tail of the file must read:

```python
def build_system_prompt(
    message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
) -> str:
    return _BASE_PROMPT + _facts_block(resolved) + _context_block(context) + _entry_block(message)


DEFINITION = AgentDefinition(
    name=AGENT_NAME,
    tool_names=TOOL_NAMES,
    build_system_prompt=build_system_prompt,
)
```

In `src/emotorad_ai/agents/motor_support.py` line 30, change:

```python
from .battery_support import _context_block, _entry_block, _facts_block
```
to
```python
from .blocks import _context_block, _entry_block, _facts_block
```
and update the comment in `build_system_prompt` from "Deliberately reuses the battery agent's context blocks" to "Deliberately reuses the shared context blocks". Remove `from typing import Any, Dict, List` if nothing else in the file uses it.

In `src/emotorad_ai/agents/dealer_orders.py`: delete the `_account_block` definition (lines 71–94), remove the `from typing import Any, Dict, List` line, and add after the `.base` import:

```python
from .blocks import _account_block
```

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass, including `tests/test_motor_support.py::InheritedBehaviourTests::test_the_context_blocks_are_shared_not_copied` and the new `tests/test_blocks.py`.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/agents/blocks.py src/emotorad_ai/agents/battery_support.py src/emotorad_ai/agents/motor_support.py src/emotorad_ai/agents/dealer_orders.py tests/test_blocks.py
git commit -m "refactor(agents): move shared prompt blocks to agents/blocks.py

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Triage takes its keyword table and supported-topics summary as inputs

**Files:**
- Modify: `src/emotorad_ai/agents/battery_support.py`, `src/emotorad_ai/agents/motor_support.py`, `src/emotorad_ai/triage.py`
- Test: `tests/test_triage_tables.py`

**Interfaces:**
- Produces: `battery_support.TOPIC = "battery"`, `battery_support.KEYWORDS: Tuple[str, ...]`, `motor_support.KEYWORDS`.
- Produces: `classify_issue(text, keywords=None)`, `topic_from_pill(pill, keywords=None)`, `TriageAgent(topic_agents, keywords=None, supported_summary=None)`. Defaults reproduce today's behaviour exactly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_triage_tables.py
"""Triage classifies against a table it is *given*, so a bot added as
configuration is routable without editing triage.py. The defaults are the
built-in agents' own tables, so nothing about them changes."""

import unittest

from emotorad_ai.agents import battery_support, motor_support
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.triage import TOPIC_KEYWORDS, TriageAgent, classify_issue, topic_from_pill

BIKE = {"frame_number": "EMXP2025004990", "product_name": "EMX Plus", "product_color": "Grey"}
BRAKES = {"battery": ("battery",), "brakes": ("brake", "braking", "ब्रेक")}


def resolved():
    return ResolvedIdentity(
        persona="customer",
        method="verified",
        identity=Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000001"),
        profile={"name": "Priya Nair"},
        bikes=[BIKE],
    )


def message(text, pill=None):
    return InboundMessage(
        "c1",
        "customer",
        Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000001"),
        "whatsapp",
        text,
        entry_metadata={"pill_clicked": pill} if pill else {},
    )


class KeywordTableTests(unittest.TestCase):
    def test_the_default_table_is_the_agent_modules_own(self):
        self.assertIs(TOPIC_KEYWORDS["battery"], battery_support.KEYWORDS)
        self.assertIs(TOPIC_KEYWORDS["motor"], motor_support.KEYWORDS)
        self.assertEqual(battery_support.TOPIC, "battery")
        self.assertEqual(motor_support.TOPIC, "motor")

    def test_classify_issue_uses_the_table_it_is_given(self):
        self.assertEqual(classify_issue("my brake is squeaking", BRAKES), "brakes")
        self.assertEqual(classify_issue("ब्रेक काम नहीं कर रहा", BRAKES), "brakes")
        self.assertIsNone(classify_issue("motor is noisy", BRAKES), "motor is not in this table")

    def test_topic_from_pill_uses_the_table_it_is_given(self):
        self.assertEqual(topic_from_pill("brakes", BRAKES), "brakes")
        self.assertEqual(topic_from_pill("brake_issue", BRAKES), "brakes")
        self.assertIsNone(topic_from_pill("motor_issue", BRAKES))


class TriageAgentTableTests(unittest.TestCase):
    def test_a_custom_topic_routes_to_its_agent(self):
        triage = TriageAgent({"brakes": "brakes_support"}, BRAKES, "brake problems")
        state = ConversationState("c1")
        outcome = triage.handle(message("the brake is squeaking"), resolved(), state)
        self.assertTrue(outcome.is_handoff)
        self.assertEqual(outcome.agent, "brakes_support")
        self.assertEqual(outcome.reason, "text->brakes")

    def test_the_unsupported_reply_names_what_is_supported(self):
        triage = TriageAgent({"brakes": "brakes_support"}, BRAKES, "brake problems")
        state = ConversationState("c1")
        outcome = triage.handle(message("battery won't charge"), resolved(), state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("I can help with brake problems from here.", outcome.reply)

    def test_the_default_summary_is_unchanged(self):
        triage = TriageAgent({"battery": "battery_support"})
        state = ConversationState("c1")
        outcome = triage.handle(message("the motor is noisy"), resolved(), state)
        self.assertIn("I can help with battery and motor problems from here.", outcome.reply)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_triage_tables -v`
Expected: FAIL with `AttributeError: module 'emotorad_ai.agents.battery_support' has no attribute 'KEYWORDS'`

- [ ] **Step 3: Declare `TOPIC` and `KEYWORDS` on the two customer agents**

In `src/emotorad_ai/agents/battery_support.py`, directly after `AGENT_NAME = "battery_support"`:

```python
TOPIC = "battery"

# Triage keywords: the cheap, deterministic path. English plus the Hindi and
# Hinglish terms that actually appear in support traffic — a Devanagari-script
# message must not be a silent miss. Note what is NOT here: bare "power". It
# appears in both "won't power on" (battery) and "power cuts out while riding"
# (drive), so on its own it classifies nothing; the phrases below carry the
# discrimination instead.
KEYWORDS = (
    "battery", "charge", "charging", "charger", "range", "backup",
    "discharge", "drain", "drains", "draining", "not turning on",
    "won't start", "wont start", "dead", "power on", "powering on",
    "no power at all", "बैटरी", "चार्ज", "batri", "charj",
)
```

In `src/emotorad_ai/agents/motor_support.py`, directly after `TOPIC = "motor"`:

```python
KEYWORDS = (
    "motor", "noise", "noisy", "sound", "grinding", "jerk", "jerking",
    "pedal assist", "pas", "throttle", "speed", "vibration",
    "cuts out", "cutting out", "cuts off", "cutting off", "power cut",
    "stops while riding", "while riding",
    "मोटर", "आवाज", "awaz",
)
```

- [ ] **Step 4: Make triage read the table it is given**

In `src/emotorad_ai/triage.py`, replace the `TOPIC_KEYWORDS` literal (the whole dict and its comment) with:

```python
from .agents import battery_support, motor_support

# The built-in table. Runtime passes the catalogue's table instead, so a bot
# added as configuration is routable without this file changing; the default
# keeps `classify_issue("...")` working for callers and tests that want today's
# behaviour.
TOPIC_KEYWORDS: Dict[str, Sequence[str]] = {
    battery_support.TOPIC: battery_support.KEYWORDS,
    motor_support.TOPIC: motor_support.KEYWORDS,
}

DEFAULT_SUPPORTED_SUMMARY = "battery and motor problems"
```

Add `Mapping` to the `typing` import. Change `classify_issue` and `topic_from_pill`:

```python
def classify_issue(
    text: str, keywords: Optional[Mapping[str, Sequence[str]]] = None
) -> Optional[str]:
    """Topic from keywords, or None when the model needs to decide.

    Returning None is a real answer, not a failure: forcing a guess here is how a
    motor complaint ends up in a battery agent, which then troubleshoots the
    wrong component confidently and at length.
    """
    table = keywords if keywords is not None else TOPIC_KEYWORDS
    lowered = text.lower()
    matched = [
        topic for topic, words in table.items()
        if any(word in lowered for word in words)
    ]
    if len(matched) != 1:
        # Zero means we do not know. More than one means the message covers two
        # components — "the motor is noisy and the battery drains fast" is two
        # issues, and picking the one with more keyword hits silently drops the
        # other. Both cases end in asking, which is cheap and correct.
        return None
    return matched[0]


def topic_from_pill(
    pill: Optional[str], keywords: Optional[Mapping[str, Sequence[str]]] = None
) -> Optional[str]:
    """Canonical topic for a channel's own pill vocabulary.

    Every channel names its entry points differently — a WhatsApp template
    `battery_issue`, an Amiigo screen `battery_health`, an IVR keypress `1`. They
    all have to land on the same topic, so the mapping lives here rather than
    each adapter guessing what triage wants to be told.

    Unmapped values fall through to keyword matching on the pill itself, which
    covers most of them without a table entry; anything left returns None and is
    resolved from the message text instead of being force-fitted.
    """
    table = keywords if keywords is not None else TOPIC_KEYWORDS
    if not pill:
        return None
    if pill in table:
        return pill
    return classify_issue(pill.replace("_", " "), table)
```

Change `TriageAgent.__init__` and the two places that use the table:

```python
class TriageAgent:
    """Greets, narrows to one bike, captures the issue, hands off."""

    def __init__(
        self,
        topic_agents: Dict[str, str],
        keywords: Optional[Mapping[str, Sequence[str]]] = None,
        supported_summary: Optional[str] = None,
    ) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
        self.keywords = keywords if keywords is not None else TOPIC_KEYWORDS
        # What the unsupported-topic reply says we *can* do. Derived from the
        # catalogue in production so a new bot is named without editing a string.
        self.supported_summary = supported_summary or DEFAULT_SUPPORTED_SUMMARY
```

In `handle`, replace:

```python
        pill = message.pill_clicked
        topic = topic_from_pill(pill) or classify_issue(text)
        source = "pill:%s" % pill if pill and topic_from_pill(pill) else "text"
```
with
```python
        pill = message.pill_clicked
        pill_topic = topic_from_pill(pill, self.keywords)
        topic = pill_topic or classify_issue(text, self.keywords)
        source = "pill:%s" % pill if pill_topic else "text"
```

In `_route_or_ask`, replace the unsupported-topic reply text:

```python
                reply=(
                    "I can help with %s from here. For anything else, "
                    "let me put you through to the support team." % self.supported_summary
                ),
```

- [ ] **Step 5: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass. `tests/test_triage.py` passes unchanged because the defaults reproduce the old table and string.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/agents/battery_support.py src/emotorad_ai/agents/motor_support.py src/emotorad_ai/triage.py tests/test_triage_tables.py
git commit -m "feat(triage): accept keyword table and supported-topics summary as inputs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `build_registry(topics=...)` drives the `search_knowledge` enum

**Files:**
- Modify: `src/emotorad_ai/tools/mocks.py:264-290` and the `search_knowledge` registration (~line 355)
- Test: `tests/test_tools_topics.py`

**Interfaces:**
- Produces: `build_registry(..., topics: Optional[Sequence[str]] = None)`. Default enum stays `["battery", "motor"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools_topics.py
import unittest

from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry


class SearchKnowledgeTopicsTests(unittest.TestCase):
    def _enum(self, registry):
        schema = registry.schemas_for([SEARCH_KNOWLEDGE])[0]
        return schema["input_schema"]["properties"]["topic"]["enum"]

    def test_the_default_enum_is_the_two_built_in_topics(self):
        self.assertEqual(self._enum(build_registry()), ["battery", "motor"])

    def test_the_enum_follows_the_topics_given(self):
        registry = build_registry(topics=("battery", "motor", "brakes"))
        self.assertEqual(self._enum(registry), ["battery", "motor", "brakes"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_tools_topics -v`
Expected: FAIL with `TypeError: build_registry() got an unexpected keyword argument 'topics'`

- [ ] **Step 3: Add the parameter**

In `build_registry`'s signature add, after `today: Optional[date] = None,`:

```python
    topics: Optional[Sequence[str]] = None,
```
(`Sequence` is imported from `typing` if not already.) After `registry = ToolRegistry()` add:

```python
    # The knowledge topics the model may narrow to. Runtime passes the bot
    # catalogue's list; a topic that has no bot is not an enum value, so the
    # model cannot search for a component nothing here can help with.
    topic_enum = list(topics) if topics else ["battery", "motor"]
```
In the `search_knowledge` registration replace `"enum": ["battery", "motor"],` with `"enum": topic_enum,`.

- [ ] **Step 4: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/tools/mocks.py tests/test_tools_topics.py
git commit -m "feat(tools): search_knowledge topic enum comes from build_registry(topics=...)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `bots.py` catalogue and `agents/generic.py`

**Files:**
- Create: `src/emotorad_ai/bots.py`, `src/emotorad_ai/agents/generic.py`
- Test: `tests/test_bots.py`

**Interfaces:**
- Produces:
  - `BotSpec(name, persona, prompt, topic=None, keywords=(), tools=(), source="draft", path=None, module=None, definition=None)` frozen dataclass; `BotSpec.to_dict()`.
  - `BotSpecError(ValueError)`.
  - `spec_from_dict(raw: Mapping, source: str, path: Optional[Path] = None) -> BotSpec` — per-spec validation.
  - `load_specs(directory: Path, source: str) -> List[BotSpec]` — every `*.yaml` under `directory`; missing directory returns `[]`.
  - `builtin_specs() -> List[BotSpec]`.
  - `BotCatalogue(specs)`; `BotCatalogue.load(published_dir=None, drafts_dir=None, extra=())`; `.specs`, `.get(name)`, `.definitions()`, `.topic_agents(persona)`, `.keywords(persona)`, `.topics()`, `.supported_summary(persona)`, `.validate(registry)`.
  - Constants `BUILTIN = "builtin"`, `PUBLISHED = "published"`, `DRAFT = "draft"`, `BOTS_DIR`, `PERSONA_TOOLS`.
  - `generic.definition_from_spec(spec) -> AgentDefinition`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bots.py
"""A bot is configuration. These tests pin what the configuration may say and
what the catalogue derives from it, so runtime, triage and the tool enum never
need a second list."""

import tempfile
import unittest
from pathlib import Path

from emotorad_ai.agents import battery_support, blocks
from emotorad_ai.agents.generic import definition_from_spec
from emotorad_ai.bots import (
    BUILTIN,
    DRAFT,
    PUBLISHED,
    BotCatalogue,
    BotSpec,
    BotSpecError,
    builtin_specs,
    load_specs,
    spec_from_dict,
)
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.tools.mocks import build_registry

BRAKES = {
    "name": "brakes_support",
    "persona": "customer",
    "topic": "brakes",
    "keywords": ["brake", "braking", "ब्रेक"],
    "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
    "prompt": "You are the brake support assistant for EMotorad.\n",
}

STOCK = {
    "name": "dealer_stock",
    "persona": "dealer",
    "topic": "stock",
    "keywords": ["stock", "availability"],
    "tools": ["get_dealer_account", "create_support_ticket"],
    "prompt": "You answer dealer stock questions.\n",
}


class SpecValidationTests(unittest.TestCase):
    def test_a_well_formed_spec_loads(self):
        spec = spec_from_dict(BRAKES, DRAFT)
        self.assertEqual(spec.name, "brakes_support")
        self.assertEqual(spec.keywords, ("brake", "braking", "ब्रेक"))
        self.assertEqual(spec.source, DRAFT)

    def test_name_must_be_snake_case(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, name="Brakes Support"), DRAFT)

    def test_persona_must_be_customer_or_dealer(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, persona="internal"), DRAFT)

    def test_prompt_must_not_be_empty(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, prompt="  "), DRAFT)

    def test_a_dealer_bot_cannot_reach_the_customer_warranty_lookup(self):
        # Dealers register most warranties under their own number; that tool
        # would hand them dozens of other people's bikes. Withheld here, not
        # merely discouraged in a prompt.
        with self.assertRaises(BotSpecError) as caught:
            spec_from_dict(dict(STOCK, tools=["lookup_warranty_record"]), DRAFT)
        self.assertIn("lookup_warranty_record", str(caught.exception))

    def test_a_customer_bot_cannot_place_dealer_orders(self):
        with self.assertRaises(BotSpecError):
            spec_from_dict(dict(BRAKES, tools=["place_order"]), DRAFT)

    def test_to_dict_round_trips(self):
        spec = spec_from_dict(BRAKES, DRAFT)
        self.assertEqual(spec_from_dict(spec.to_dict(), DRAFT), spec)


class CatalogueTests(unittest.TestCase):
    def test_built_ins_are_present_with_their_definitions(self):
        catalogue = BotCatalogue(builtin_specs())
        names = [s.name for s in catalogue.specs]
        self.assertEqual(
            names, ["battery_support", "motor_support", "late_warranty_registration", "dealer_orders"]
        )
        self.assertIs(catalogue.definitions()["battery_support"], battery_support.DEFINITION)
        self.assertTrue(all(s.source == BUILTIN for s in catalogue.specs))

    def test_derived_tables_for_the_built_ins_match_today(self):
        catalogue = BotCatalogue(builtin_specs())
        self.assertEqual(catalogue.topic_agents("customer"), {"battery": "battery_support", "motor": "motor_support"})
        self.assertIs(catalogue.keywords("customer")["battery"], battery_support.KEYWORDS)
        self.assertEqual(catalogue.topics(), ["battery", "motor"])
        self.assertEqual(catalogue.supported_summary("customer"), "battery and motor problems")
        self.assertEqual(catalogue.topic_agents("dealer"), {})

    def test_a_yaml_bot_is_derived_into_every_table(self):
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(BRAKES, DRAFT), spec_from_dict(STOCK, DRAFT)])
        self.assertEqual(catalogue.topic_agents("customer")["brakes"], "brakes_support")
        self.assertEqual(catalogue.keywords("customer")["brakes"], ("brake", "braking", "ब्रेक"))
        self.assertEqual(catalogue.topics(), ["battery", "brakes", "motor"])
        self.assertEqual(catalogue.supported_summary("customer"), "battery, motor and brakes problems")
        self.assertEqual(catalogue.topic_agents("dealer"), {"stock": "dealer_stock"})
        self.assertIn("brakes_support", catalogue.definitions())

    def test_topics_only_include_bots_that_search_knowledge(self):
        # dealer_orders has a topic but no search tool, so it is not an enum value.
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(STOCK, DRAFT)])
        self.assertNotIn("stock", catalogue.topics())
        self.assertNotIn("order", catalogue.topics())

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(BotSpecError):
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, name="battery_support"), DRAFT)])

    def test_duplicate_topics_are_rejected(self):
        with self.assertRaises(BotSpecError):
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, topic="battery"), DRAFT)])

    def test_overlapping_keywords_within_a_persona_are_rejected(self):
        # An overlap makes classify_issue return None for both — neither bot is
        # ever reached, and nothing says why.
        with self.assertRaises(BotSpecError) as caught:
            BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, keywords=["brake", "battery"]), DRAFT)])
        self.assertIn("battery", str(caught.exception))

    def test_the_same_keyword_across_personas_is_fine(self):
        BotCatalogue(builtin_specs() + [spec_from_dict(dict(STOCK, keywords=["battery"]), DRAFT)])

    def test_validate_rejects_a_tool_the_registry_does_not_have(self):
        catalogue = BotCatalogue(builtin_specs() + [spec_from_dict(dict(BRAKES, tools=["get_battery_diagnostics"]), DRAFT)])
        with self.assertRaises(BotSpecError):
            catalogue.validate(build_registry(diagnostics_available=False))
        catalogue.validate(build_registry(diagnostics_available=True))

    def test_validate_does_not_police_built_ins(self):
        # battery_support lists get_battery_diagnostics on purpose; Agent.run
        # filters it out when telematics do not exist.
        BotCatalogue(builtin_specs()).validate(build_registry(diagnostics_available=False))

    def test_get_raises_for_an_unknown_bot(self):
        with self.assertRaises(KeyError):
            BotCatalogue(builtin_specs()).get("nope")


class LoadingTests(unittest.TestCase):
    def test_specs_load_from_yaml_files_and_a_missing_directory_is_empty(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(load_specs(root / "missing", PUBLISHED), [])
            (root / "brakes_support.yaml").write_text(yaml.safe_dump(BRAKES), encoding="utf-8")
            specs = load_specs(root, PUBLISHED)
            self.assertEqual([s.name for s in specs], ["brakes_support"])
            self.assertEqual(specs[0].source, PUBLISHED)
            self.assertEqual(specs[0].path, root / "brakes_support.yaml")

    def test_catalogue_load_combines_published_and_drafts(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            published = Path(tmp) / "bots"
            drafts = Path(tmp) / "drafts"
            (drafts / "bots").mkdir(parents=True)
            published.mkdir()
            (published / "brakes_support.yaml").write_text(yaml.safe_dump(BRAKES), encoding="utf-8")
            (drafts / "bots" / "dealer_stock.yaml").write_text(yaml.safe_dump(STOCK), encoding="utf-8")
            catalogue = BotCatalogue.load(published_dir=published, drafts_dir=drafts)
            sources = {s.name: s.source for s in catalogue.specs}
            self.assertEqual(sources["brakes_support"], PUBLISHED)
            self.assertEqual(sources["dealer_stock"], DRAFT)
            self.assertEqual(sources["battery_support"], BUILTIN)


class GenericDefinitionTests(unittest.TestCase):
    def _message(self, pill=None):
        return InboundMessage(
            "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", "hi",
            entry_metadata={"pill_clicked": pill} if pill else {},
        )

    def test_a_customer_spec_gets_the_shared_customer_blocks(self):
        definition = definition_from_spec(spec_from_dict(BRAKES, DRAFT))
        self.assertEqual(definition.name, "brakes_support")
        self.assertEqual(definition.tool_names, tuple(BRAKES["tools"]))
        resolved = ResolvedIdentity(
            persona="customer", method="verified",
            identity=Identity(strength=VERIFIED, phone="+919876543210"),
            profile={"name": "Ananya Rao"},
            bikes=[{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": True,
                    "months_remaining": 10, "term_months": 24}],
        )
        prompt = definition.build_system_prompt(self._message("brake_issue"), resolved, "Visited /brakes")
        self.assertTrue(prompt.startswith(BRAKES["prompt"]))
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)
        self.assertIn("Visited /brakes", prompt)
        self.assertIn("'brake_issue'", prompt)

    def test_a_dealer_spec_gets_the_account_block(self):
        definition = definition_from_spec(spec_from_dict(STOCK, DRAFT))
        resolved = ResolvedIdentity(
            persona="dealer", method="verified",
            identity=Identity(strength=VERIFIED, phone="+919000000001"),
            profile={"dealer_id": "DLR-PUN-014", "name": "Royal Cycle Stores", "city": "Pune",
                     "credit_limit": 500000, "credit_used": 380000, "payment_terms_days": 30,
                     "overdue_amount": 0, "status": "active"},
        )
        prompt = definition.build_system_prompt(self._message(), resolved, "")
        self.assertIn("Royal Cycle Stores", prompt)
        self.assertIn("Credit available: 120000", prompt)

    def test_the_blocks_are_shared_not_copied(self):
        definition = definition_from_spec(spec_from_dict(BRAKES, DRAFT))
        self.assertIs(definition.build_system_prompt.__globals__["_facts_block"], blocks._facts_block)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_bots -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.agents.generic'`

- [ ] **Step 3: Write `agents/generic.py`**

```python
# src/emotorad_ai/agents/generic.py
"""A sub-agent from a spec instead of a module.

The four hand-written agents proved the shape: a name, a tool slice, and a
prompt that ends with the persona's shared blocks. Everything else — identity,
enrichment, triage, guardrails, the coverage post-check, disclosure, idempotency
— is inherited from the loop in `base.py` and never restated here. A bot defined
in YAML is therefore exactly as safe as one defined in Python, because the parts
that make it safe were never in the Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from .base import AgentDefinition
from .blocks import _account_block, _context_block, _entry_block, _facts_block

if TYPE_CHECKING:  # bots.py imports this module; avoid the cycle at import time
    from ..bots import BotSpec


def definition_from_spec(spec: "BotSpec") -> AgentDefinition:
    prompt = spec.prompt

    if spec.persona == "dealer":

        def build_system_prompt(
            message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
        ) -> str:
            return prompt + _account_block(resolved)

    else:

        def build_system_prompt(
            message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
        ) -> str:
            return prompt + _facts_block(resolved) + _context_block(context) + _entry_block(message)

    return AgentDefinition(
        name=spec.name,
        tool_names=tuple(spec.tools),
        build_system_prompt=build_system_prompt,
    )
```

- [ ] **Step 4: Write `bots.py`**

```python
# src/emotorad_ai/bots.py
"""The bot catalogue — every sub-agent the runtime can route to, in one list.

A bot is configuration: a name, a persona, a topic, the keywords triage matches
on, the tools it may call, and a prompt. The four built-in agents are entries in
the same list as the YAML ones, so runtime, triage, the search_knowledge topic
enum and the "I can help with …" reply all derive from here and nothing about a
bot is written down twice.

Files, not a database: `bots/*.yaml` is published (Git is the audit trail, a PR
is the approval); a drafts directory holds what the playground is trying out.
Both load through the same validation, which raises rather than skips — a spec
that fails to load is a bot that silently stopped existing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .agents import battery_support, dealer_orders, late_warranty, motor_support
from .agents.base import AgentDefinition
from .agents.generic import definition_from_spec
from .tools.mocks import (
    BOOK_SERVICE_SLOT,
    CREATE_SUPPORT_TICKET,
    FIND_SERVICE_SLOTS,
    GET_BATTERY_DIAGNOSTICS,
    GET_DEALER_ACCOUNT,
    LOOKUP_WARRANTY_RECORD,
    PLACE_ORDER,
    QUOTE_ORDER,
    SEARCH_KNOWLEDGE,
    SUBMIT_WARRANTY_PROOF,
)

BOTS_DIR = Path(__file__).resolve().parent.parent.parent / "bots"

BUILTIN = "builtin"
PUBLISHED = "published"
DRAFT = "draft"

PERSONAS = ("customer", "dealer")

# What each persona's bots may call, enforced at load. A spec can narrow this;
# it can never widen it. The dealer list has no warranty lookup on purpose:
# dealers register most warranties under their own number, so that tool would
# hand them dozens of unrelated customers' bikes (see agents/dealer_orders.py).
PERSONA_TOOLS: Dict[str, frozenset] = {
    "customer": frozenset(
        {
            LOOKUP_WARRANTY_RECORD,
            GET_BATTERY_DIAGNOSTICS,
            SEARCH_KNOWLEDGE,
            CREATE_SUPPORT_TICKET,
            FIND_SERVICE_SLOTS,
            BOOK_SERVICE_SLOT,
            SUBMIT_WARRANTY_PROOF,
        }
    ),
    "dealer": frozenset(
        {GET_DEALER_ACCOUNT, QUOTE_ORDER, PLACE_ORDER, CREATE_SUPPORT_TICKET, SEARCH_KNOWLEDGE}
    ),
}

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class BotSpecError(ValueError):
    """A spec is malformed or conflicts with another. Raised at load, never later."""


@dataclass(frozen=True)
class BotSpec:
    name: str
    persona: str
    prompt: str
    topic: Optional[str] = None
    keywords: Sequence[str] = ()
    tools: Sequence[str] = ()
    source: str = DRAFT
    path: Optional[Path] = None
    # Built-ins only: the module whose `_BASE_PROMPT` the playground may swap.
    module: Optional[str] = None
    # Built-ins carry their own definition; YAML bots are built by generic.py.
    definition: Optional[AgentDefinition] = field(default=None, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "persona": self.persona,
            "topic": self.topic,
            "keywords": list(self.keywords),
            "tools": list(self.tools),
            "prompt": self.prompt,
        }


def spec_from_dict(raw: Mapping[str, Any], source: str, path: Optional[Path] = None) -> BotSpec:
    where = str(path) if path else "<inline>"
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise BotSpecError("%s: name must be snake_case (got %r)" % (where, name))

    persona = raw.get("persona")
    if persona not in PERSONAS:
        raise BotSpecError("%s: persona must be one of %s (got %r)" % (where, PERSONAS, persona))

    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BotSpecError("%s: prompt must not be empty" % where)

    topic = raw.get("topic")
    if topic is not None and (not isinstance(topic, str) or not _NAME.match(topic)):
        raise BotSpecError("%s: topic must be snake_case (got %r)" % (where, topic))

    keywords = tuple(str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip())
    tools = tuple(str(t) for t in (raw.get("tools") or []))

    allowed = PERSONA_TOOLS[persona]
    forbidden = [t for t in tools if t not in allowed]
    if forbidden:
        raise BotSpecError(
            "%s: a %s bot may not use %s (allowed: %s)"
            % (where, persona, ", ".join(forbidden), ", ".join(sorted(allowed)))
        )

    return BotSpec(
        name=name,
        persona=persona,
        prompt=prompt,
        topic=topic,
        keywords=keywords,
        tools=tools,
        source=source,
        path=path,
    )


def load_specs(directory: Path, source: str) -> List[BotSpec]:
    """Every `*.yaml` directly under `directory`. A missing directory is empty,
    not an error — the drafts directory does not exist until the first draft."""
    root = Path(directory)
    if not root.exists():
        return []
    import yaml

    specs: List[BotSpec] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise BotSpecError("%s: expected a mapping at the top level" % path)
        specs.append(spec_from_dict(raw, source, path))
    return specs


def builtin_specs() -> List[BotSpec]:
    """The four hand-written agents, as catalogue entries.

    Order matters: it is the order the playground lists them and the order
    `supported_summary` names them, and "battery and motor" is what the reply
    said before this file existed.
    """
    return [
        BotSpec(
            name=battery_support.AGENT_NAME,
            persona="customer",
            prompt=battery_support._BASE_PROMPT,
            topic=battery_support.TOPIC,
            keywords=battery_support.KEYWORDS,
            tools=battery_support.TOOL_NAMES,
            source=BUILTIN,
            module=battery_support.__name__,
            definition=battery_support.DEFINITION,
        ),
        BotSpec(
            name=motor_support.AGENT_NAME,
            persona="customer",
            prompt=motor_support._BASE_PROMPT,
            topic=motor_support.TOPIC,
            keywords=motor_support.KEYWORDS,
            tools=motor_support.TOOL_NAMES,
            source=BUILTIN,
            module=motor_support.__name__,
            definition=motor_support.DEFINITION,
        ),
        BotSpec(
            name=late_warranty.AGENT_NAME,
            persona="customer",
            prompt=late_warranty._BASE_PROMPT,
            tools=late_warranty.TOOL_NAMES,
            source=BUILTIN,
            module=late_warranty.__name__,
            definition=late_warranty.DEFINITION,
        ),
        BotSpec(
            name=dealer_orders.AGENT_NAME,
            persona="dealer",
            prompt=dealer_orders._BASE_PROMPT,
            topic="order",
            tools=dealer_orders.TOOL_NAMES,
            source=BUILTIN,
            module=dealer_orders.__name__,
            definition=dealer_orders.DEFINITION,
        ),
    ]


class BotCatalogue:
    def __init__(self, specs: Iterable[BotSpec]) -> None:
        self._specs = list(specs)
        self._check_unique()
        self._check_keywords_disjoint()

    @classmethod
    def load(
        cls,
        published_dir: Optional[Path] = None,
        drafts_dir: Optional[Path] = None,
        extra: Sequence[BotSpec] = (),
    ) -> "BotCatalogue":
        specs = builtin_specs()
        specs += load_specs(Path(published_dir) if published_dir else BOTS_DIR, PUBLISHED)
        if drafts_dir:
            specs += load_specs(Path(drafts_dir) / "bots", DRAFT)
        specs += list(extra)
        return cls(specs)

    # -- validation ------------------------------------------------------------

    def _check_unique(self) -> None:
        seen_names: Dict[str, BotSpec] = {}
        seen_topics: Dict[str, BotSpec] = {}
        for spec in self._specs:
            if spec.name in seen_names:
                raise BotSpecError(
                    "duplicate bot name %r (%s and %s)"
                    % (spec.name, _where(seen_names[spec.name]), _where(spec))
                )
            seen_names[spec.name] = spec
            if spec.topic:
                key = "%s:%s" % (spec.persona, spec.topic)
                if key in seen_topics:
                    raise BotSpecError(
                        "duplicate %s topic %r (%s and %s)"
                        % (spec.persona, spec.topic, _where(seen_topics[key]), _where(spec))
                    )
                seen_topics[key] = spec

    def _check_keywords_disjoint(self) -> None:
        # Two bots of one persona sharing a keyword makes classify_issue return
        # None for both, and neither is ever reached. Across personas it is fine:
        # the tables are never consulted together.
        for persona in PERSONAS:
            owner: Dict[str, BotSpec] = {}
            for spec in self._specs:
                if spec.persona != persona:
                    continue
                for word in spec.keywords:
                    lowered = word.lower()
                    if lowered in owner and owner[lowered].name != spec.name:
                        raise BotSpecError(
                            "keyword %r is used by both %s and %s"
                            % (word, owner[lowered].name, spec.name)
                        )
                    owner[lowered] = spec

    def validate(self, registry: Any) -> None:
        """Every tool a YAML bot names must exist in this registry.

        Built-ins are exempt: battery_support lists get_battery_diagnostics on
        purpose, and Agent.run drops it when telematics do not exist.
        """
        for spec in self._specs:
            if spec.source == BUILTIN:
                continue
            missing = [t for t in spec.tools if t not in registry.specs]
            if missing:
                raise BotSpecError(
                    "%s: tool(s) not registered: %s" % (_where(spec), ", ".join(missing))
                )

    # -- derived tables --------------------------------------------------------

    @property
    def specs(self) -> List[BotSpec]:
        return list(self._specs)

    def get(self, name: str) -> BotSpec:
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def definitions(self) -> Dict[str, AgentDefinition]:
        return {
            spec.name: spec.definition if spec.definition is not None else definition_from_spec(spec)
            for spec in self._specs
        }

    def topic_agents(self, persona: str) -> Dict[str, str]:
        """topic -> bot name, for the bots triage can reach by keyword."""
        return {
            spec.topic: spec.name
            for spec in self._specs
            if spec.persona == persona and spec.topic and spec.keywords
        }

    def keywords(self, persona: str) -> Dict[str, Sequence[str]]:
        return {
            spec.topic: spec.keywords
            for spec in self._specs
            if spec.persona == persona and spec.topic and spec.keywords
        }

    def topics(self) -> List[str]:
        """Knowledge topics, for the search_knowledge enum: every topic of a bot
        that can actually search. dealer_orders has a topic and no search tool,
        so it is not one."""
        return sorted(
            {spec.topic for spec in self._specs if spec.topic and SEARCH_KNOWLEDGE in spec.tools}
        )

    def supported_summary(self, persona: str) -> str:
        topics = [t for t in self.topic_agents(persona)]
        if not topics:
            return "general questions"
        if len(topics) == 1:
            return "%s problems" % topics[0]
        return "%s and %s problems" % (", ".join(topics[:-1]), topics[-1])


def _where(spec: BotSpec) -> str:
    return str(spec.path) if spec.path else "%s:%s" % (spec.source, spec.name)
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_bots -v`
Expected: all pass. Then `python3 -m unittest discover -s tests -t .` — all pass.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/bots.py src/emotorad_ai/agents/generic.py tests/test_bots.py
git commit -m "feat(bots): bot catalogue with YAML specs, validation and a generic agent

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Runtime is wired from the catalogue, dealers route by keyword

**Files:**
- Modify: `src/emotorad_ai/runtime.py`
- Test: `tests/test_generic_agent.py`

**Interfaces:**
- Consumes: `BotCatalogue`, `classify_issue(text, keywords)`, `TriageAgent(topic_agents, keywords, supported_summary)`, `build_registry(topics=...)`.
- Produces: `Runtime(settings=None, registry=None, llm=None, log=None, resolver=None, diagnostics_available=False, catalogue=None)`; attribute `runtime.catalogue`. `TOPIC_AGENTS` and `DEALER_AGENTS` constants removed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generic_agent.py
"""A bot defined in YAML, run through the real skeleton.

Mirrors tests/test_motor_support.py::InheritedBehaviourTests on purpose: if a
YAML bot inherits the same things a hand-written second bot did, the catalogue
has not opened a way around any of them.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import DealerWhatsAppAdapter, WhatsAppAdapter
from emotorad_ai.bots import DRAFT, BotCatalogue, builtin_specs, spec_from_dict
from emotorad_ai.config import Settings
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)

BRAKES = spec_from_dict(
    {
        "name": "brakes_support",
        "persona": "customer",
        "topic": "brakes",
        "keywords": ["brake", "braking", "ब्रेक"],
        "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        "prompt": "You are the brake support assistant for EMotorad.\n",
    },
    DRAFT,
)

STOCK = spec_from_dict(
    {
        "name": "dealer_stock",
        "persona": "dealer",
        "topic": "stock",
        "keywords": ["stock", "availability"],
        "tools": ["get_dealer_account", "create_support_ticket"],
        "prompt": "You answer dealer stock questions.\n",
    },
    DRAFT,
)


def make_runtime(script, extra=(BRAKES, STOCK)):
    catalogue = BotCatalogue(builtin_specs() + list(extra))
    registry = build_registry(today=TODAY, topics=catalogue.topics())
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry,
        llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
        catalogue=catalogue,
    )
    return runtime, llm


def whatsapp(runtime, sender, text, conversation_id="c1"):
    adapter = WhatsAppAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id}))


def dealer_says(runtime, sender, text, conversation_id="d1"):
    adapter = DealerWhatsAppAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id}))


class RoutingTests(unittest.TestCase):
    def test_a_yaml_bot_is_reached_on_its_keywords(self):
        runtime, llm = make_runtime(
            [call_tool(SEARCH_KNOWLEDGE, {"query": "squeak", "topic": "brakes"}, "t1"), say("Let me help.")]
        )
        reply = whatsapp(runtime, "919876543210", "my brake squeaks when I stop slowly")
        self.assertEqual(reply.handled_by, "brakes_support")
        self.assertEqual(llm.requests[0]["tools"][0]["name"], "lookup_warranty_record")
        self.assertEqual(
            [t["name"] for t in llm.requests[0]["tools"]],
            ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        )

    def test_built_in_routing_is_unchanged(self):
        runtime, _ = make_runtime([say("Try another socket.")])
        self.assertEqual(whatsapp(runtime, "919876543210", "battery won't charge").handled_by, "battery_support")

    def test_the_search_enum_includes_the_new_topic(self):
        runtime, llm = make_runtime([say("ok")])
        whatsapp(runtime, "919876543210", "brake squeaks")
        search = next(t for t in llm.requests[0]["tools"] if t["name"] == SEARCH_KNOWLEDGE)
        self.assertEqual(search["input_schema"]["properties"]["topic"]["enum"], ["battery", "brakes", "motor"])

    def test_the_unsupported_reply_names_every_customer_topic(self):
        runtime, llm = make_runtime([], extra=(BRAKES,))
        # "seat" is classified by nothing, so triage asks; make it a topic with
        # no agent by giving triage a table entry with no bot behind it.
        runtime.triage.keywords = dict(runtime.triage.keywords, seat=("seat",))
        reply = whatsapp(runtime, "919876543210", "the seat is loose")
        self.assertEqual(llm.requests, [])
        self.assertIn("battery, motor and brakes problems", reply.text)

    def test_the_default_catalogue_is_the_built_ins_plus_bots_dir(self):
        registry = build_registry(today=TODAY)
        runtime = Runtime(
            settings=Settings(log_to_stdout=False, log_path=None),
            registry=registry, llm=ScriptedClaude([]), log=EventLog(path=None, to_stdout=False),
            resolver=IdentityResolver(registry),
        )
        self.assertIn("battery_support", runtime.agents)
        self.assertIn("dealer_orders", runtime.agents)


class InheritedBehaviourTests(unittest.TestCase):
    def test_it_inherits_customer_context_verbatim(self):
        runtime, llm = make_runtime([say("Let me help.")])
        whatsapp(runtime, "919876543210", "brake squeaks")
        prompt = llm.requests[0]["system"]
        self.assertTrue(prompt.startswith(BRAKES.prompt))
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)
        self.assertIn("in warranty", prompt)

    def test_it_inherits_multi_bike_disambiguation(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "brake is squeaking")
        self.assertEqual(llm.requests, [])
        self.assertIn("Which one", reply.text)

    def test_it_inherits_the_ai_disclosure(self):
        runtime, _ = make_runtime([say("Let me help.")])
        self.assertTrue(has_disclosure(whatsapp(runtime, "919876543210", "brake squeaks").text))

    def test_it_inherits_the_coverage_post_check(self):
        runtime, _ = make_runtime(
            [call_tool("lookup_warranty_record", {}, "t1"),
             say("Your brakes are still covered under warranty, so no charge.")]
        )
        reply = whatsapp(runtime, "919812345678", "brake squeaks, is it covered")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertTrue(reply.escalated)

    def test_it_inherits_the_human_handoff(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "brake squeaks, connect me to an agent")
        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")

    def test_it_inherits_the_safety_gate(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "my brakes are not working")
        self.assertEqual(llm.requests, [])
        self.assertTrue(reply.escalated)


class DealerRoutingTests(unittest.TestCase):
    def test_a_dealer_yaml_bot_is_reached_on_its_keywords(self):
        runtime, llm = make_runtime([say("EMX Plus is in stock.")])
        reply = dealer_says(runtime, "919000000001", "is the EMX Plus in stock")
        self.assertEqual(reply.handled_by, "dealer_stock")
        self.assertEqual([t["name"] for t in llm.requests[0]["tools"]], ["get_dealer_account", "create_support_ticket"])

    def test_unmatched_dealer_messages_still_go_to_dealer_orders(self):
        runtime, _ = make_runtime([say("How many would you like?")])
        self.assertEqual(dealer_says(runtime, "919000000001", "need 5 EMX Plus").handled_by, "dealer_orders")

    def test_a_dealer_conversation_stays_with_its_agent(self):
        runtime, _ = make_runtime([say("In stock."), say("Yes, 40 units.")])
        dealer_says(runtime, "919000000001", "is the EMX Plus in stock")
        reply = dealer_says(runtime, "919000000001", "need 5")  # "need" is an orders phrase
        self.assertEqual(reply.handled_by, "dealer_stock")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_generic_agent -v`
Expected: FAIL with `TypeError: Runtime.__init__() got an unexpected keyword argument 'catalogue'`

- [ ] **Step 3: Rewire `runtime.py`**

Replace the imports block from `from .agents.base import Agent, AgentDefinition` down to `from .agents.motor_support import DEFINITION as MOTOR_SUPPORT_DEFINITION` with:

```python
from .agents.base import Agent
from .agents.battery_support import AGENT_NAME as BATTERY_SUPPORT
from .agents.dealer_orders import AGENT_NAME as DEALER_ORDERS
from .agents.late_warranty import AGENT_NAME as LATE_WARRANTY
from .bots import BotCatalogue
```

Change `from .triage import TriageAgent` to `from .triage import TriageAgent, classify_issue`.

Delete the `TOPIC_AGENTS` / `DEALER_AGENTS` constants and their comment. Replace `Runtime.__init__` with:

```python
class Runtime:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[ToolRegistry] = None,
        llm: Any = None,
        log: Optional[EventLog] = None,
        resolver: Optional[IdentityResolver] = None,
        diagnostics_available: bool = False,
        catalogue: Optional[BotCatalogue] = None,
    ) -> None:
        self.settings = settings or load_settings()
        # Built-ins plus bots/*.yaml. Drafts are never loaded here — the
        # playground passes its own catalogue.
        self.catalogue = catalogue or BotCatalogue.load()
        self.registry = registry or build_registry(
            diagnostics_available=diagnostics_available, topics=self.catalogue.topics()
        )
        self.catalogue.validate(self.registry)
        self.log = log or EventLog(path=self.settings.log_path, to_stdout=self.settings.log_to_stdout)
        self.llm = llm if llm is not None else BedrockClaude(self.settings)
        self.resolver = resolver or IdentityResolver(self.registry)
        self.conversations = ConversationStore()
        self.enricher = ContextEnricher()

        # Topic -> sub-agent, **scoped per persona**. Never one router over
        # everything: a dealer and a customer asking the same words mean
        # different things, and a shared agent set is how a dealer reaches a
        # customer-only tool.
        self.triage = TriageAgent(
            self.catalogue.topic_agents("customer"),
            self.catalogue.keywords("customer"),
            self.catalogue.supported_summary("customer"),
        )
        self._dealer_agents = self.catalogue.topic_agents("dealer")
        self._dealer_keywords = self.catalogue.keywords("dealer")

        self.agents = {
            name: Agent(definition, self.registry, self.llm, self.log, self.settings)
            for name, definition in self.catalogue.definitions().items()
        }
```

Replace the dealer branch in `handle`:

```python
        if resolved.persona == "dealer":
            # Dealers bypass customer triage entirely. There is no bike to
            # disambiguate and no customer record to enrich from — and routing
            # them through the customer path is precisely how a dealer would end
            # up holding someone else's warranty data. Keyword match against the
            # dealer bots; anything unmatched is an order, which is what dealers
            # mostly want.
            if state.agent is None:
                topic = classify_issue(message.message_text, self._dealer_keywords)
                agent_name = self._dealer_agents.get(topic or "", DEALER_ORDERS)
                state.route_to(agent_name)
                self.log.routed(
                    message.conversation_id, agent_name,
                    "persona:dealer" + (":%s" % topic if topic else ""),
                )
            return self._run_agent(state.agent or DEALER_ORDERS, message, resolved, state)
```

- [ ] **Step 4: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass. `tests/test_dealer_orders.py` passes because with no dealer keywords every dealer message still lands on `dealer_orders` on the first turn, and stays there.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/runtime.py tests/test_generic_agent.py
git commit -m "feat(runtime): derive agents, triage and dealer routing from the bot catalogue

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `AnthropicClaude` and `StaticResolver`

**Files:**
- Modify: `src/emotorad_ai/llm.py:41-88`, `src/emotorad_ai/identity.py` (append after `IdentityResolver`)
- Test: `tests/test_llm_anthropic.py`, `tests/test_identity_graph.py` (append one class)

**Interfaces:**
- Produces: `AnthropicClaude(settings, api_key, model=None, client=None)` with `.create(system, messages, tools) -> LLMResponse`; `DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"`; module function `response_to_llm(response) -> LLMResponse` shared with `BedrockClaude`.
- Produces: `StaticResolver(resolved)` with `.hydrate(message) -> ResolvedIdentity`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_anthropic.py
"""The playground's model client: the first-party API instead of Bedrock, with
the identical request shape, so a bot tuned in the playground behaves the same
once it is served from Bedrock."""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.llm import DEFAULT_ANTHROPIC_MODEL, AnthropicClaude, BedrockClaude


class _Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)}


class _Usage:
    def model_dump(self):
        return {"input_tokens": 12, "output_tokens": 7}


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _Client:
    def __init__(self, response):
        self.messages = _Messages(response)


TEXT = _Response([_Block(type="text", text="Try another socket.")])
TOOL = _Response(
    [_Block(type="text", text="Checking."), _Block(type="tool_use", id="tu_1", name="search_knowledge", input={"query": "x"})],
    stop_reason="tool_use",
)


class AnthropicClaudeTests(unittest.TestCase):
    def test_it_sends_the_same_request_shape_as_bedrock(self):
        client = _Client(TEXT)
        llm = AnthropicClaude(Settings(effort="low"), api_key="k", model="claude-sonnet-5", client=client)
        llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}])
        call = client.messages.calls[0]
        self.assertEqual(call["model"], "claude-sonnet-5")
        self.assertEqual(call["system"], "sys")
        self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(call["tools"], [{"name": "t"}])
        self.assertEqual(call["thinking"], {"type": "adaptive"})
        self.assertEqual(call["output_config"], {"effort": "low"})

    def test_the_default_model_is_opus_5(self):
        llm = AnthropicClaude(Settings(), api_key="k", client=_Client(TEXT))
        llm.create(system="s", messages=[], tools=[])
        self.assertEqual(DEFAULT_ANTHROPIC_MODEL, "claude-opus-5")
        self.assertEqual(llm.model, "claude-opus-5")

    def test_text_and_tool_calls_map_like_bedrock(self):
        anthropic_llm = AnthropicClaude(Settings(), api_key="k", client=_Client(TOOL))
        bedrock_llm = BedrockClaude(Settings(), client=_Client(TOOL))
        a = anthropic_llm.create(system="s", messages=[], tools=[])
        b = bedrock_llm.create(system="s", messages=[], tools=[])
        self.assertEqual(a, b)
        self.assertTrue(a.wants_tools)
        self.assertEqual(a.tool_uses[0].name, "search_knowledge")
        self.assertEqual(a.tool_uses[0].arguments, {"query": "x"})
        self.assertEqual(a.text, "Checking.")
        self.assertEqual(a.usage, {"input_tokens": 12, "output_tokens": 7})


if __name__ == "__main__":
    unittest.main()
```

Append to `tests/test_identity_graph.py`:

```python


class StaticResolverTests(unittest.TestCase):
    def test_it_returns_the_identity_it_was_given_for_any_message(self):
        from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
        from emotorad_ai.identity import ResolvedIdentity, StaticResolver

        pinned = ResolvedIdentity(
            persona="customer", method="verified",
            identity=Identity(strength=VERIFIED, phone="+919999999999"),
            profile={"name": "Test Customer"}, bikes=[],
        )
        resolver = StaticResolver(pinned)
        message = InboundMessage("c1", "customer", Identity(), "website_chat", "hi")
        self.assertIs(resolver.hydrate(message), pinned)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_llm_anthropic tests.test_identity_graph -v`
Expected: FAIL with `ImportError: cannot import name 'AnthropicClaude'` and `ImportError: cannot import name 'StaticResolver'`.

- [ ] **Step 3: Implement `AnthropicClaude` and share the response mapping**

In `src/emotorad_ai/llm.py`, replace the whole `BedrockClaude` class with:

```python
def response_to_llm(response: Any) -> LLMResponse:
    """The SDK message -> our LLMResponse. One mapping for both clients, so the
    playground and production cannot disagree about what a tool call looks like."""
    api_content: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    tool_uses: List[ToolUse] = []
    for block in response.content:
        api_content.append(block.model_dump(exclude_none=True))
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append(ToolUse(id=block.id, name=block.name, arguments=dict(block.input or {})))

    return LLMResponse(
        stop_reason=response.stop_reason or "end_turn",
        text="\n".join(part for part in text_parts if part).strip(),
        tool_uses=tool_uses,
        api_content=api_content,
        usage=response.usage.model_dump() if response.usage else None,
    )


class BedrockClaude:
    """Claude via Bedrock, in Emotorad's own AWS account and region."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        if client is not None:
            self._client = client
        else:
            from anthropic import AnthropicBedrockMantle  # imported lazily: tests never need it

            self._client = AnthropicBedrockMantle(aws_region=settings.aws_region)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        response = self._client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            messages=list(messages),
            tools=list(tools),
            # Adaptive thinking with a low effort default: battery triage is a
            # bounded problem and the turn is in front of a waiting customer.
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
        )
        return response_to_llm(response)


DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"


class AnthropicClaude:
    """Claude via the first-party Anthropic API — the playground's path.

    Production stays on Bedrock. This exists so an internal user with an API key
    can drive a bot through the identical agent loop without AWS credentials.
    Same request shape as BedrockClaude on purpose: a bot tuned here must behave
    the same once it is served from Bedrock. The key is held for the session and
    never logged.
    """

    def __init__(
        self, settings: Settings, api_key: str, model: Optional[str] = None, client: Any = None
    ) -> None:
        self.settings = settings
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        if client is not None:
            self._client = client
        else:
            import anthropic  # imported lazily: tests never need it

            self._client = anthropic.Anthropic(api_key=api_key)

    def create(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> LLMResponse:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            messages=list(messages),
            tools=list(tools),
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
        )
        return response_to_llm(response)
```

- [ ] **Step 4: Add `StaticResolver` to `identity.py`**

Append after the `IdentityResolver` class (before `replace_customer_id`):

```python
class StaticResolver:
    """Returns one pre-built ResolvedIdentity for every message.

    For the playground: the rider is chosen in the sidebar, not resolved from a
    session token, so hydration is a lookup of what was chosen. Presets still go
    through the real IdentityResolver *once* to build that value, so they stay
    honest as the mocks evolve; this only pins the answer for the session.
    """

    def __init__(self, resolved: ResolvedIdentity) -> None:
        self._resolved = resolved

    def hydrate(self, message: InboundMessage) -> ResolvedIdentity:
        return self._resolved
```

- [ ] **Step 5: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/llm.py src/emotorad_ai/identity.py tests/test_llm_anthropic.py tests/test_identity_graph.py
git commit -m "feat(llm): AnthropicClaude client for the playground; StaticResolver

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Attachments reach the model through the agent loop

**Files:**
- Modify: `src/emotorad_ai/agents/base.py:80` (the `history.append` of the user turn)
- Test: `tests/test_agent_attachments.py`

**Interfaces:**
- Produces: `base.user_content(message: InboundMessage) -> Union[str, List[Dict[str, Any]]]`. A text-only message still produces the plain string it does today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_attachments.py
"""An uploaded image or PDF has to reach the model as a content block, or the
playground's upload widget is testing nothing. Text-only turns keep the exact
history shape they had, so nothing about prefix caching or the offline planner
moves."""

import unittest

from emotorad_ai.agents.base import user_content
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage


def message(text, attachments=()):
    return InboundMessage(
        "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", text,
        attachments=list(attachments),
    )


class UserContentTests(unittest.TestCase):
    def test_text_only_is_still_a_plain_string(self):
        self.assertEqual(user_content(message("battery won't charge")), "battery won't charge")

    def test_a_data_url_image_becomes_a_base64_block_before_the_text(self):
        content = user_content(
            message("what is this light", [Attachment(kind="image", url="data:image/png;base64,AAAA", mime_type="image/png")])
        )
        self.assertEqual(
            content,
            [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {"type": "text", "text": "what is this light"},
            ],
        )

    def test_a_pdf_becomes_a_document_block(self):
        content = user_content(
            message("", [Attachment(kind="document", url="data:application/pdf;base64,QUJD", mime_type="application/pdf")])
        )
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
        # The API rejects an empty text block, so an attachment with no words
        # still carries one.
        self.assertEqual(content[1], {"type": "text", "text": "(attachment)"})

    def test_an_http_image_is_passed_by_url(self):
        content = user_content(message("see", [Attachment(kind="image", url="https://cdn.test/a.jpg", mime_type="image/jpeg")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.jpg"}})

    def test_unknown_types_are_dropped_not_sent(self):
        content = user_content(message("hi", [Attachment(kind="document", url="data:text/csv;base64,QQ==", mime_type="text/csv")]))
        self.assertEqual(content, "hi")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_agent_attachments -v`
Expected: FAIL with `ImportError: cannot import name 'user_content'`

- [ ] **Step 3: Implement it and use it in the loop**

In `src/emotorad_ai/agents/base.py`, add `Union` to the `typing` import and `Attachment` to the `..contract` import. Add before `class Agent`:

```python
def user_content(message: InboundMessage) -> Union[str, List[Dict[str, Any]]]:
    """What this turn contributes to the model history.

    A plain string when there are no attachments — unchanged from before, so
    text-only conversations keep their exact history shape. Otherwise a list of
    blocks with the text last, so the model reads the picture before the
    question about it.
    """
    blocks: List[Dict[str, Any]] = []
    for attachment in message.attachments:
        block = _attachment_block(attachment)
        if block is not None:
            blocks.append(block)
    if not blocks:
        return message.message_text
    # The API rejects an empty text block.
    blocks.append({"type": "text", "text": message.message_text or "(attachment)"})
    return blocks


def _attachment_block(attachment: Attachment) -> Optional[Dict[str, Any]]:
    mime_type = attachment.mime_type or ""
    if attachment.url.startswith("data:"):
        header, _, data = attachment.url.partition(",")
        mime_type = mime_type or header[len("data:"):].split(";")[0]
        source: Dict[str, Any] = {"type": "base64", "media_type": mime_type, "data": data}
    else:
        source = {"type": "url", "url": attachment.url}

    if mime_type.startswith("image/"):
        return {"type": "image", "source": source}
    if mime_type == "application/pdf":
        return {"type": "document", "source": source}
    # Anything else is not something the model can read; dropping it beats a 400.
    return None
```

In `Agent.run`, change `history.append({"role": "user", "content": message.message_text})` to `history.append({"role": "user", "content": user_content(message)})`.

- [ ] **Step 4: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/agents/base.py tests/test_agent_attachments.py
git commit -m "feat(agents): attachments become image/document blocks in the model history

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `PlaygroundSession` runs the real runtime with a trace

**Files:**
- Create: `src/emotorad_ai/playground_runtime.py`
- Test: `tests/test_playground_runtime.py`

**Interfaces:**
- Consumes: `Runtime(catalogue=...)`, `StaticResolver`, `build_registry(topics=..., knowledge_base=...)`, `KnowledgeBase(records=...)`, `load_records(directory)`, `definition_from_spec`, `BotSpec.module`.
- Produces:
  - `knowledge_base_with_drafts(drafts_dir: Optional[Path]) -> KnowledgeBase`.
  - `TurnResult(reply: Reply, events: List[Dict[str, Any]])`.
  - `PlaygroundSession(bot: str, resolved: ResolvedIdentity, channel: str, catalogue: BotCatalogue, llm, oms_available=True, today=None, drafts_dir=None)` with `.send(text, pill=None, attachments=()) -> TurnResult`, `.set_prompt(text)`, `.system_prompt_preview() -> str`, `.spec`, `.conversation_id`.
  - `TRACE_EVENTS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_playground_runtime.py
"""The playground drives the same Runtime production does. These tests pin
that: routing, tools, guardrails and disclosure all happen, and the trace
reports what the platform decided."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from emotorad_ai.bots import DRAFT, BotCatalogue, builtin_specs, spec_from_dict
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.knowledge import KnowledgeError
from emotorad_ai.llm import OfflinePlanner, ScriptedClaude, call_tool, say
from emotorad_ai.playground_runtime import PlaygroundSession, knowledge_base_with_drafts
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)

BRAKES = spec_from_dict(
    {
        "name": "brakes_support",
        "persona": "customer",
        "topic": "brakes",
        "keywords": ["brake", "braking"],
        "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
        "prompt": "You are the brake support assistant.\n",
    },
    DRAFT,
)


def ananya():
    registry = build_registry(today=TODAY)
    message = InboundMessage(
        "preview", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", ""
    )
    return IdentityResolver(registry).hydrate(message)


def session(script, bot="battery_support", extra=(BRAKES,)):
    catalogue = BotCatalogue(builtin_specs() + list(extra))
    return PlaygroundSession(
        bot=bot, resolved=ananya(), channel="website_chat", catalogue=catalogue,
        llm=ScriptedClaude(script), today=TODAY,
    )


class SessionTests(unittest.TestCase):
    def test_a_turn_runs_triage_tools_and_disclosure(self):
        s = session([call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"), say("Try another socket.")])
        turn = s.send("battery won't charge")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        self.assertIn("Try another socket.", turn.reply.text)
        kinds = [e["event"] for e in turn.events]
        self.assertIn("routed", kinds)
        self.assertIn("tool_call", kinds)
        self.assertEqual(turn.reply.metadata["tool_calls"], [SEARCH_KNOWLEDGE])

    def test_the_conversation_persists_across_turns(self):
        s = session([say("First."), say("Second.")])
        s.send("battery won't charge")
        turn = s.send("still nothing")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        state = s.runtime.conversations.get(s.conversation_id)
        self.assertEqual(state.turns, 2)

    def test_a_pill_skips_classification(self):
        s = session([say("Which light is on?")])
        turn = s.send("hi", pill="battery_issue")
        routed = next(e for e in turn.events if e["event"] == "routed")
        self.assertEqual(routed["reason"], "pill:battery_issue->battery")

    def test_guardrails_run_before_the_model(self):
        s = session([])
        turn = s.send("the battery is swelling")
        self.assertTrue(turn.reply.escalated)
        self.assertEqual(s.runtime.llm.requests, [])
        self.assertIn("guardrail", [e["event"] for e in turn.events])

    def test_a_draft_bot_is_selectable_and_reached(self):
        s = session([say("Let me help with the brake.")], bot="brakes_support")
        turn = s.send("my brake squeaks")
        self.assertEqual(turn.reply.handled_by, "brakes_support")

    def test_set_prompt_on_a_yaml_bot_takes_effect_without_losing_the_conversation(self):
        s = session([say("one"), say("two")], bot="brakes_support")
        s.send("brake squeaks")
        s.set_prompt("You are a terse brake assistant.\n")
        s.send("still squeaking")
        self.assertTrue(s.runtime.llm.requests[1]["system"].startswith("You are a terse brake assistant."))
        self.assertEqual(s.runtime.conversations.get(s.conversation_id).turns, 2)

    def test_set_prompt_on_a_built_in_is_scoped_to_the_call(self):
        from emotorad_ai.agents import battery_support

        original = battery_support._BASE_PROMPT
        s = session([say("ok")])
        s.set_prompt("You are a terse battery assistant.\n")
        s.send("battery won't charge")
        self.assertTrue(s.runtime.llm.requests[0]["system"].startswith("You are a terse battery assistant."))
        self.assertIs(battery_support._BASE_PROMPT, original)

    def test_system_prompt_preview_matches_what_the_model_gets(self):
        s = session([say("ok")])
        preview = s.system_prompt_preview()
        s.send("battery won't charge")
        self.assertEqual(preview, s.runtime.llm.requests[0]["system"])

    def test_it_works_with_the_offline_planner(self):
        catalogue = BotCatalogue(builtin_specs())
        s = PlaygroundSession(
            bot="battery_support", resolved=ananya(), channel="website_chat",
            catalogue=catalogue, llm=OfflinePlanner(), today=TODAY,
        )
        turn = s.send("battery won't charge")
        self.assertEqual(turn.reply.handled_by, "battery_support")
        self.assertFalse(turn.reply.escalated)


class DraftKnowledgeTests(unittest.TestCase):
    RECORD = (
        "id: brakes-squeak\ntitle: Brakes squeak\ntopic: brakes\n"
        "symptoms: [squeak, squeal]\nsteps: [Check the pads for glazing.]\n"
    )

    def test_draft_records_are_added_to_the_published_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            (drafts / "knowledge" / "brakes").mkdir(parents=True)
            (drafts / "knowledge" / "brakes" / "squeak.yaml").write_text(self.RECORD, encoding="utf-8")
            kb = knowledge_base_with_drafts(drafts)
            self.assertIn("brakes-squeak", {r.id for r in kb.records})
            self.assertIn("motor-noise", {r.id for r in kb.records})

    def test_no_drafts_directory_is_just_the_published_set(self):
        self.assertEqual(
            {r.id for r in knowledge_base_with_drafts(None).records},
            {r.id for r in knowledge_base_with_drafts(Path("/nonexistent")).records},
        )

    def test_a_draft_id_that_shadows_a_published_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            (drafts / "knowledge" / "motor").mkdir(parents=True)
            (drafts / "knowledge" / "motor" / "noise.yaml").write_text(
                self.RECORD.replace("brakes-squeak", "motor-noise"), encoding="utf-8"
            )
            with self.assertRaises(KnowledgeError):
                knowledge_base_with_drafts(drafts)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_playground_runtime -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.playground_runtime'`

- [ ] **Step 3: Write `playground_runtime.py`**

```python
# src/emotorad_ai/playground_runtime.py
"""The playground's runtime: production's `Runtime`, driven per session.

What differs from `api.py` is exactly three inputs — which model client, which
rider, and which catalogue (drafts included) — and nothing in between. Triage,
tools, the safety and coverage guardrails and the disclosure all run, because a
prompt-only preview that skips them is a plausible-looking wrong answer.

Streamlit-free on purpose, so all of this is unit-testable.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .agents.base import Agent
from .agents.generic import definition_from_spec
from .bots import BUILTIN, BotCatalogue, BotSpec
from .config import Settings
from .contract import Attachment, InboundMessage, Reply, new_conversation_id
from .identity import ResolvedIdentity, StaticResolver
from .knowledge import KnowledgeBase, KnowledgeError, load_records
from .observability import EventLog
from .runtime import Runtime
from .tools.mocks import build_registry

# What the trace panel shows per turn: what the platform decided, not the prose.
TRACE_EVENTS = ("routed", "classification", "guardrail", "tool_call", "escalation", "llm_turn")


def knowledge_base_with_drafts(drafts_dir: Optional[Path]) -> KnowledgeBase:
    """Published records plus whatever the drafts directory holds.

    A draft may not reuse a published id: it would silently double the record,
    and the copy that retrieves is whichever sorts first.
    """
    records = load_records()
    extra = Path(drafts_dir) / "knowledge" if drafts_dir else None
    if extra is not None and extra.exists():
        drafts = load_records(extra)
        published = {record.id for record in records}
        clash = [record.id for record in drafts if record.id in published]
        if clash:
            raise KnowledgeError("draft knowledge id(s) already published: %s" % ", ".join(clash))
        records = records + drafts
    return KnowledgeBase(records=records)


@dataclass
class TurnResult:
    reply: Reply
    events: List[Dict[str, Any]]


class PlaygroundSession:
    def __init__(
        self,
        bot: str,
        resolved: ResolvedIdentity,
        channel: str,
        catalogue: BotCatalogue,
        llm: Any,
        oms_available: bool = True,
        today: Optional[date] = None,
        drafts_dir: Optional[Path] = None,
    ) -> None:
        self.spec: BotSpec = catalogue.get(bot)
        self.resolved = resolved
        self.channel = channel
        self.catalogue = catalogue
        self.settings = Settings(log_path=None, log_to_stdout=False)
        self.log = EventLog(path=None, to_stdout=False)
        registry = build_registry(
            oms_available=oms_available,
            today=today or date.today(),
            topics=catalogue.topics(),
            knowledge_base=knowledge_base_with_drafts(drafts_dir),
            knowledge_bike=resolved.single_bike,
        )
        self.runtime = Runtime(
            settings=self.settings,
            registry=registry,
            llm=llm,
            log=self.log,
            resolver=StaticResolver(resolved),
            catalogue=catalogue,
        )
        self.conversation_id = new_conversation_id()
        self.prompt_override: Optional[str] = None

    # -- prompt editing ----------------------------------------------------------

    def set_prompt(self, text: str) -> None:
        """Use `text` as the bot's base prompt from the next turn on.

        A YAML bot's prompt is captured in its definition, so the agent is
        rebuilt in place — the conversation is kept. A built-in reads its
        module's `_BASE_PROMPT` at call time, so that is patched around each
        call instead (and restored, so nothing leaks into the process).
        """
        self.prompt_override = text
        if self.spec.source != BUILTIN:
            definition = definition_from_spec(replace(self.spec, prompt=text))
            self.runtime.agents[self.spec.name] = Agent(
                definition, self.runtime.registry, self.runtime.llm, self.log, self.settings
            )

    @contextmanager
    def _patched_builtin_prompt(self) -> Iterator[None]:
        if self.prompt_override is None or self.spec.source != BUILTIN or not self.spec.module:
            yield
            return
        module = importlib.import_module(self.spec.module)
        original = module._BASE_PROMPT
        module._BASE_PROMPT = self.prompt_override
        try:
            yield
        finally:
            module._BASE_PROMPT = original

    def system_prompt_preview(self, pill: Optional[str] = None) -> str:
        message = self._message("", pill, ())
        state = self.runtime.conversations.get(self.conversation_id)
        context = state.context_block or ""
        with self._patched_builtin_prompt():
            definition = self.runtime.agents[self.spec.name].definition
            return definition.build_system_prompt(message, self.resolved, context)

    # -- turns ---------------------------------------------------------------------

    def send(
        self, text: str, pill: Optional[str] = None, attachments: Sequence[Attachment] = ()
    ) -> TurnResult:
        message = self._message(text, pill, attachments)
        before = len(self.log.events)
        with self._patched_builtin_prompt():
            reply = self.runtime.handle(message)
        events = [event for event in self.log.events[before:] if event["event"] in TRACE_EVENTS]
        return TurnResult(reply=reply, events=events)

    def _message(
        self, text: str, pill: Optional[str], attachments: Sequence[Attachment]
    ) -> InboundMessage:
        return InboundMessage(
            conversation_id=self.conversation_id,
            persona=self.resolved.persona,
            identity=self.resolved.identity,
            channel=self.channel,
            message_text=text.strip(),
            entry_metadata={"pill_clicked": pill} if pill else {},
            attachments=list(attachments),
        )
```

- [ ] **Step 4: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/playground_runtime.py tests/test_playground_runtime.py
git commit -m "feat(playground): PlaygroundSession drives the real Runtime with a trace

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Draft and export helpers, `bots/README.md`

**Files:**
- Create: `src/emotorad_ai/playground_bots.py`, `bots/README.md`
- Modify: `docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md:154` (drafts default path)
- Test: `tests/test_playground_bots.py`

**Interfaces:**
- Produces:
  - `DRAFTS_DIR: Path` (from `EMOTORAD_AI_BOT_DRAFTS`, default `<repo>/.playground/drafts`), `EXPORT_DIR = <repo>/.playground/export`.
  - `prompt_template(persona) -> str`.
  - `load_catalogue(drafts_dir) -> BotCatalogue`.
  - `save_draft(raw: Mapping, drafts_dir: Path) -> Path` — validates against built-ins + published + other drafts; raises `BotSpecError`.
  - `save_draft_knowledge(topic: str, records: Sequence[Mapping], drafts_dir: Path) -> List[Path]` — raises `KnowledgeError`.
  - `delete_draft(name: str, drafts_dir: Path) -> None`.
  - `export_for_review(name: str, drafts_dir: Path, export_dir: Path) -> List[Path]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_playground_bots.py
import tempfile
import unittest
from pathlib import Path

from emotorad_ai.bots import DRAFT, BotSpecError
from emotorad_ai.knowledge import KnowledgeError, load_records
from emotorad_ai.playground_bots import (
    delete_draft,
    export_for_review,
    load_catalogue,
    prompt_template,
    save_draft,
    save_draft_knowledge,
)

BRAKES = {
    "name": "brakes_support",
    "persona": "customer",
    "topic": "brakes",
    "keywords": ["brake", "braking"],
    "tools": ["lookup_warranty_record", "search_knowledge", "create_support_ticket"],
    "prompt": "You are the brake support assistant.\nBe brief.\n",
}

RECORD = {
    "id": "brakes-squeak",
    "title": "Brakes squeak",
    "symptoms": ["squeak", "squeal"],
    "steps": ["Check the pads for glazing."],
    "escalate_when": "Metal-on-metal grinding.",
}


class DraftTests(unittest.TestCase):
    def test_save_draft_writes_yaml_the_catalogue_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            path = save_draft(BRAKES, drafts)
            self.assertEqual(path, drafts / "bots" / "brakes_support.yaml")
            text = path.read_text(encoding="utf-8")
            self.assertIn("prompt: |", text, "multi-line prompts are written in block style so they are reviewable")
            spec = load_catalogue(drafts).get("brakes_support")
            self.assertEqual(spec.source, DRAFT)
            self.assertEqual(spec.prompt, BRAKES["prompt"])

    def test_save_draft_rejects_a_conflict_with_a_built_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BotSpecError):
                save_draft(dict(BRAKES, keywords=["brake", "motor"]), Path(tmp))
            self.assertFalse((Path(tmp) / "bots" / "brakes_support.yaml").exists(), "nothing is written on failure")

    def test_save_draft_overwrites_the_same_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            save_draft(dict(BRAKES, prompt="Shorter.\n"), drafts)
            self.assertEqual(load_catalogue(drafts).get("brakes_support").prompt, "Shorter.\n")

    def test_delete_draft_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            save_draft(BRAKES, drafts)
            delete_draft("brakes_support", drafts)
            with self.assertRaises(KeyError):
                load_catalogue(drafts).get("brakes_support")

    def test_draft_knowledge_is_written_in_the_repo_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp)
            paths = save_draft_knowledge("brakes", [RECORD], drafts)
            self.assertEqual(paths, [drafts / "knowledge" / "brakes" / "brakes-squeak.yaml"])
            records = load_records(drafts / "knowledge")
            self.assertEqual(records[0].topic, "brakes")
            self.assertEqual(records[0].symptoms, ("squeak", "squeal"))

    def test_draft_knowledge_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KnowledgeError):
                save_draft_knowledge("brakes", [dict(RECORD, steps=[])], Path(tmp))

    def test_export_copies_spec_and_knowledge_into_the_export_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = Path(tmp) / "drafts"
            export = Path(tmp) / "export"
            save_draft(BRAKES, drafts)
            save_draft_knowledge("brakes", [RECORD], drafts)
            paths = export_for_review("brakes_support", drafts, export)
            self.assertEqual(
                sorted(p.relative_to(export).as_posix() for p in paths),
                ["bots/brakes_support.yaml", "knowledge/brakes/brakes-squeak.yaml"],
            )

    def test_prompt_templates_carry_the_persona_rules(self):
        self.assertIn("search_knowledge", prompt_template("customer"))
        self.assertIn("Never ask the customer for their bike model", prompt_template("customer"))
        self.assertIn("Never state a price", prompt_template("dealer"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_playground_bots -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.playground_bots'`

- [ ] **Step 3: Write `playground_bots.py`**

```python
# src/emotorad_ai/playground_bots.py
"""Draft bots: where the playground writes, and how they get to a PR.

Drafts live outside the tree (`EMOTORAD_AI_BOT_DRAFTS`, default
`.playground/drafts`, gitignored and bind-mounted on staging). "Export for
review" copies a draft and its knowledge into `.playground/export/` laid out
exactly as `bots/` and `knowledge/` are, so an engineer moves the files into
place and opens a PR. Nothing here touches a tracked file — same convention as
the prompt diff the playground already produced.

Streamlit-free on purpose, so all of this is unit-testable.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import yaml

from .bots import BOTS_DIR, DRAFT, BotCatalogue, BotSpecError, spec_from_dict
from .knowledge import KnowledgeError, load_records

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DRAFTS_DIR = Path(os.environ.get("EMOTORAD_AI_BOT_DRAFTS", str(REPO_ROOT / ".playground" / "drafts")))
EXPORT_DIR = REPO_ROOT / ".playground" / "export"

_CUSTOMER_TEMPLATE = """\
You are the {topic} support assistant for EMotorad, an Indian e-cycle company. You are \
talking to a signed-in customer about their bike.

Your job: understand the symptom, walk the customer through safe checks, and either \
resolve the issue or raise a support ticket. Be warm, plain-spoken and brief.

How to work:
- Ask at most one or two clarifying questions, and only when the answer changes what you \
would suggest.
- Never ask the customer for their bike model, purchase date or warranty status. Those are \
given to you below.
- Every factual claim about how the bike behaves must come from search_knowledge, called \
with topic "{topic}". If it returns nothing useful, say you are not certain and raise a \
ticket rather than guessing.
- Suggest at most two or three checks at a time, and only ones that are safe for a customer \
to do themselves.
- Ownership and warranty coverage come only from lookup_warranty_record. Never estimate or \
infer either.
- If you cannot resolve it, call create_support_ticket with a clean summary of the symptom, \
what was already tried, and the result. Tell the customer the ticket number. Do not promise \
a specific outcome, refund, replacement or repair cost.

Style: reply in short plain sentences suited to a chat widget. No headings, no bullet \
symbols, no markdown, no emoji. Indian English. If you do not know something, say so.
"""

_DEALER_TEMPLATE = """\
You are EMotorad's dealer assistant for {topic}. You are talking to a verified dealer on \
their WhatsApp line.

How to work:
- Be brief and specific. Dealers are working.
- Never state a price, discount, total, credit figure or stock count that did not come from \
a tool. You have no authority to set or negotiate any of them.
- If the dealer asks for an exception, say that is not something you can approve and offer \
to raise it with their account manager.
- If you cannot help, call create_support_ticket with a clean summary.

Style: short plain sentences suited to WhatsApp. Indian English. No headings, no markdown, \
no emoji.
"""


def prompt_template(persona: str, topic: str = "<topic>") -> str:
    template = _DEALER_TEMPLATE if persona == "dealer" else _CUSTOMER_TEMPLATE
    return template.replace("{topic}", topic)


class _Dumper(yaml.SafeDumper):
    """Block style for multi-line strings, so a prompt diff reads as prose."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _dump(data: Mapping[str, Any]) -> str:
    return yaml.dump(dict(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def load_catalogue(drafts_dir: Path) -> BotCatalogue:
    return BotCatalogue.load(published_dir=BOTS_DIR, drafts_dir=drafts_dir)


def save_draft(raw: Mapping[str, Any], drafts_dir: Path) -> Path:
    """Validate against everything else that exists, then write. Nothing is
    written on failure, so a bad save cannot leave a draft that will not load."""
    spec = spec_from_dict(raw, DRAFT)
    others = [s for s in load_catalogue(drafts_dir).specs if s.name != spec.name]
    BotCatalogue(others + [spec])  # raises BotSpecError on any conflict

    directory = Path(drafts_dir) / "bots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s.yaml" % spec.name)
    path.write_text(_dump(spec.to_dict()), encoding="utf-8")
    return path


def delete_draft(name: str, drafts_dir: Path) -> None:
    path = Path(drafts_dir) / "bots" / ("%s.yaml" % name)
    if path.exists():
        path.unlink()


def save_draft_knowledge(topic: str, records: Sequence[Mapping[str, Any]], drafts_dir: Path) -> List[Path]:
    """Write records in the `knowledge/README.md` format, then load them back
    through the real loader so a malformed record fails here, not at retrieval."""
    directory = Path(drafts_dir) / "knowledge" / topic
    directory.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    try:
        for record in records:
            payload: Dict[str, Any] = {
                "id": record.get("id"),
                "title": record.get("title"),
                "topic": topic,
                "symptoms": list(record.get("symptoms") or []),
                "applies_to": dict(record.get("applies_to") or {}),
                "source": record.get("source", "Authored in the playground"),
                "media": list(record.get("media") or []),
                "steps": list(record.get("steps") or []),
                "escalate_when": record.get("escalate_when", ""),
            }
            if not payload["id"]:
                raise KnowledgeError("a knowledge record needs an id")
            path = directory / ("%s.yaml" % payload["id"])
            path.write_text(_dump(payload), encoding="utf-8")
            written.append(path)
        load_records(Path(drafts_dir) / "knowledge")
    except KnowledgeError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return written


def export_for_review(name: str, drafts_dir: Path, export_dir: Path) -> List[Path]:
    """Copy a draft and its knowledge into `export_dir` laid out like the repo."""
    spec = load_catalogue(drafts_dir).get(name)
    if spec.source != DRAFT or spec.path is None:
        raise BotSpecError("%s is not a draft" % name)

    copied: List[Path] = []
    target = Path(export_dir) / "bots" / spec.path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec.path, target)
    copied.append(target)

    if spec.topic:
        source_dir = Path(drafts_dir) / "knowledge" / spec.topic
        if source_dir.exists():
            for path in sorted(source_dir.glob("*.yaml")):
                destination = Path(export_dir) / "knowledge" / spec.topic / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                copied.append(destination)
    return copied
```

- [ ] **Step 4: Write `bots/README.md`**

```markdown
# Published bots

One YAML file per sub-agent. A file here is live: the runtime loads every
`*.yaml` in this directory at startup, alongside the four built-in agents
(`src/emotorad_ai/agents/`). Git is the audit trail and a PR is the approval —
same convention as `knowledge/`.

Drafts do not live here. The playground writes them under
`EMOTORAD_AI_BOT_DRAFTS` (default `.playground/drafts/`, gitignored) and
"Export for review" copies a finished draft and its knowledge records into
`.playground/export/`, laid out as `bots/` and `knowledge/`. Move them into
place and open a PR.

## Format

```yaml
name: brakes_support        # snake_case, unique; appears as handled_by in logs
persona: customer           # customer | dealer
topic: brakes               # knowledge topic AND triage topic; unique per persona
keywords:                   # triage; English plus the Hindi/Hinglish forms seen in traffic
  - brake
  - braking
  - ब्रेक
tools:                      # from the registry; the persona allowlist is enforced at load
  - lookup_warranty_record
  - search_knowledge
  - create_support_ticket
prompt: |
  You are the brake support assistant for EMotorad ...
```

## Rules (enforced at load — a bad file stops the service, on purpose)

- `name` and `topic` are unique across built-ins, published bots and drafts.
- Every tool exists and is allowed for the persona. Dealer bots can never reach
  `lookup_warranty_record`; the allowlists are in `src/emotorad_ai/bots.py`.
- Keywords do not overlap with any other bot of the same persona: an overlap
  makes triage refuse to choose, and neither bot is ever reached.
- A bot with `search_knowledge` needs records under `knowledge/<topic>/`.

The safety branch, the human handoff, the coverage post-check and the AI
disclosure are not in this file and cannot be changed from it.
```

- [ ] **Step 5: Fix the drafts default path in the spec**

In `docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md`, change the line `- Drafts directory from `EMOTORAD_AI_BOT_DRAFTS`, default `.playground/bots` locally.` to `- Drafts directory from `EMOTORAD_AI_BOT_DRAFTS`, default `.playground/drafts` locally (holding `bots/` and `knowledge/`).`

- [ ] **Step 6: Run the suite**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/playground_bots.py bots/README.md tests/test_playground_bots.py docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md
git commit -m "feat(playground): draft bot save/export helpers and bots/ README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: The playground UI — Chat mode on the real runtime, New bot mode

**Files:**
- Create: `src/emotorad_ai/playground_riders.py`
- Rewrite: `src/emotorad_ai/playground.py`

**Interfaces:**
- Consumes: everything from Tasks 8 and 9, `AnthropicClaude`, `OfflinePlanner`.
- Produces: `playground_riders.CUSTOMER_SCENARIOS`, `DEALER_SCENARIOS`, `Scenario`, `scenarios_for(persona)`, `resolved_for_preset(scenario)`, `custom_customer_form()`, `custom_dealer_form()` (the last two use Streamlit).

No unit test: this is Streamlit. Verification is Step 4.

- [ ] **Step 1: Move the rider code into `playground_riders.py`**

```python
# src/emotorad_ai/playground_riders.py
"""Who the "customer" is for a playground turn.

Presets are named fixtures from tools/fixtures.py — the same data the automated
tests run against — hydrated through the real IdentityResolver so they stay
honest as the mocks evolve. Custom riders are typed in on the spot; coverage is
still computed by the real `_coverage()` helper, never re-implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import streamlit as st

from .contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from .identity import IdentityResolver, ResolvedIdentity
from .tools import fixtures
from .tools.mocks import _coverage, build_registry


@dataclass(frozen=True)
class Scenario:
    label: str
    persona: str
    channel: str
    phone: Optional[str]
    strength: str
    oms_available: bool = True


CUSTOMER_SCENARIOS = [
    Scenario("Single bike, in warranty (Ananya)", "customer", "website_chat", "+919876543210", VERIFIED),
    Scenario("Single bike, out of warranty (Rohit)", "customer", "website_chat", "+919812345678", VERIFIED),
    Scenario("Multi-bike customer (Priya, 3 bikes)", "customer", "whatsapp", "+919700000001", VERIFIED),
    Scenario("Unregistered — no warranty record", "customer", "website_chat", fixtures.PHONE_WITH_NO_RECORD, VERIFIED),
    Scenario("OMS system down (outage)", "customer", "website_chat", "+919876543210", VERIFIED, oms_available=False),
    Scenario("Anonymous / not signed in", "customer", "website_chat", None, ANONYMOUS),
]

DEALER_SCENARIOS = [
    Scenario("Dealer, healthy credit (Royal Cycle Stores)", "dealer", "dealer_app", "+919000000001", VERIFIED),
    Scenario("Dealer, near credit limit with overdue", "dealer", "dealer_app", "+919000000002", VERIFIED),
    Scenario("Dealer on hold", "dealer", "dealer_app", "+919000000003", VERIFIED),
]


def scenarios_for(persona: str) -> List[Scenario]:
    return DEALER_SCENARIOS if persona == "dealer" else CUSTOMER_SCENARIOS


def resolved_for_preset(scenario: Scenario) -> ResolvedIdentity:
    identity = Identity(strength=scenario.strength, phone=scenario.phone)
    message = InboundMessage(
        conversation_id="preview",
        persona=scenario.persona,
        identity=identity,
        channel=scenario.channel,
        message_text="",
    )
    registry = build_registry(oms_available=scenario.oms_available, today=date.today())
    return IdentityResolver(registry).hydrate(message)


def _resolved_for_custom_customer(
    name: str, phone: Optional[str], verified: bool, bike_rows: List[Dict[str, Any]]
) -> ResolvedIdentity:
    identity = Identity(strength=VERIFIED if verified else ANONYMOUS, phone=phone if verified else None)
    if not verified:
        return ResolvedIdentity(persona="customer", method="unverified", identity=identity)
    if not bike_rows:
        return ResolvedIdentity(
            persona="customer", method="no_warranty_record", identity=identity, error="no_warranty_record"
        )
    bikes = [_coverage(row, date.today()) for row in bike_rows]
    return ResolvedIdentity(
        persona="customer", method="verified", identity=identity, profile={"name": name}, bikes=bikes
    )


def custom_customer_form() -> ResolvedIdentity:
    name = st.text_input("Name", "Test Customer")
    phone = st.text_input("Phone", "+919999999999")
    verified = st.checkbox("Verified (signed in)", value=True)
    bike_rows: List[Dict[str, Any]] = []
    if verified:
        bike_count = st.number_input("Bikes owned", min_value=0, max_value=5, value=1, step=1)
        for i in range(int(bike_count)):
            with st.expander("Bike %d" % (i + 1), expanded=(bike_count == 1)):
                product_name = st.text_input("Model", "EMX Plus", key="custom_bike_model_%d" % i)
                purchase_date = st.date_input("Purchase date", value=date(2025, 6, 1), key="custom_bike_date_%d" % i)
                frame_number = st.text_input("Frame number", "CUSTOM%03d" % i, key="custom_bike_frame_%d" % i)
                battery_variant = st.text_input("Battery variant (optional)", "", key="custom_bike_batt_%d" % i)
                bike_rows.append(
                    {
                        "frame_number": frame_number,
                        "product_name": product_name,
                        "purchase_date": purchase_date.isoformat(),
                        "battery_variant": battery_variant,
                        "product_color": "",
                    }
                )
    return _resolved_for_custom_customer(name, phone, verified, bike_rows)


def custom_dealer_form() -> ResolvedIdentity:
    name = st.text_input("Dealer name", "Test Cycle Stores")
    phone = st.text_input("Phone", "+919999999999")
    city = st.text_input("City", "Pune")
    credit_limit = st.number_input("Credit limit (₹)", min_value=0, value=500000, step=10000)
    credit_used = st.number_input("Credit used (₹)", min_value=0, value=100000, step=10000)
    overdue = st.number_input("Overdue amount (₹)", min_value=0, value=0, step=1000)
    terms_days = st.number_input("Payment terms (days)", min_value=0, value=30, step=5)
    profile = {
        "dealer_id": "CUSTOM-%s" % phone[-4:],
        "name": name,
        "city": city,
        "credit_limit": int(credit_limit),
        "credit_used": int(credit_used),
        "payment_terms_days": int(terms_days),
        "overdue_amount": int(overdue),
        "status": "active",
    }
    return ResolvedIdentity(
        persona="dealer", method="verified", identity=Identity(strength=VERIFIED, phone=phone), profile=profile
    )
```

- [ ] **Step 2: Rewrite `playground.py`**

```python
# src/emotorad_ai/playground.py
"""The playground: add a bot, tune its prompt, and test it through the real
runtime ("AIRO v0" — the deferred agent-builder, at the size the build plan
said to start with).

Run locally:

    streamlit run src/emotorad_ai/playground.py

Two modes:

  Chat     — pick any bot (built-in, published, or draft), a rider and a model,
             and talk to it. Every turn runs production's `Runtime`: identity,
             enrichment, the safety and handoff guardrails, triage with bike
             selection, the sub-agent tool loop against the mocked tools, the
             coverage post-check and the AI disclosure. The prompt editor
             changes the bot's base prompt from the next turn on.
  New bot  — a form that writes a draft spec (and optional knowledge records)
             into the drafts directory. The draft is immediately selectable in
             Chat. "Export for review" copies it into `.playground/export/` for
             an engineer to move into `bots/` and `knowledge/` via PR.

What this is not: a way to add tools, guardrails or ticket categories, or to
change production behaviour. Nothing here writes to a tracked file.
"""

from __future__ import annotations

import base64
import difflib
import importlib
import mimetypes
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

# `streamlit run` executes this file as a script, so `src/` is never on sys.path
# the way an installed package would be — same bootstrap tests/__init__.py uses.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from emotorad_ai.bots import BUILTIN, DRAFT, PERSONA_TOOLS, BotSpecError
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.knowledge import KnowledgeError
from emotorad_ai.llm import AnthropicClaude, OfflinePlanner
from emotorad_ai.playground_bots import (
    DRAFTS_DIR,
    EXPORT_DIR,
    delete_draft,
    export_for_review,
    load_catalogue,
    prompt_template,
    save_draft,
    save_draft_knowledge,
)
from emotorad_ai.playground_riders import (
    custom_customer_form,
    custom_dealer_form,
    resolved_for_preset,
    scenarios_for,
)
from emotorad_ai.playground_runtime import PlaygroundSession

# Both support adaptive thinking and effort, which the runtime always sends.
MODELS = {
    "Opus 5 (production model)": "claude-opus-5",
    "Sonnet 5 (faster iteration)": "claude-sonnet-5",
    "Offline planner (no model, no key)": None,
}

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent.parent / ".playground"


# --- helpers -------------------------------------------------------------------


def _save_prompt_diff(agent_name: str, original_prompt: str, edited_prompt: str) -> Path:
    diff_lines = difflib.unified_diff(
        original_prompt.splitlines(keepends=True),
        edited_prompt.splitlines(keepends=True),
        fromfile="%s/_BASE_PROMPT (current)" % agent_name,
        tofile="%s/_BASE_PROMPT (edited in playground)" % agent_name,
    )
    diff_text = "".join(diff_lines) or "# no changes\n"
    PLAYGROUND_DIR.mkdir(exist_ok=True)
    path = PLAYGROUND_DIR / ("%s_%s.patch" % (agent_name, date.today().isoformat()))
    path.write_text(diff_text)
    return path


def _attachment_from_upload(uploaded: Any) -> Attachment:
    name = uploaded.name or "upload"
    mime_type = uploaded.type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    data = base64.b64encode(uploaded.getvalue()).decode("utf-8")
    return Attachment(
        kind="image" if mime_type.startswith("image/") else "document",
        url="data:%s;base64,%s" % (mime_type, data),
        mime_type=mime_type,
    )


def _make_llm(model_id: Optional[str], api_key: str) -> Any:
    if model_id is None:
        return OfflinePlanner()
    return AnthropicClaude(Settings(), api_key=api_key, model=model_id)


def _new_session(bot: str, resolved: Any, channel: str, oms_available: bool, llm: Any) -> PlaygroundSession:
    return PlaygroundSession(
        bot=bot,
        resolved=resolved,
        channel=channel,
        catalogue=load_catalogue(DRAFTS_DIR),
        llm=llm,
        oms_available=oms_available,
        drafts_dir=DRAFTS_DIR,
    )


def _source_tag(source: str) -> str:
    return {"builtin": "built-in", "published": "published", "draft": "draft"}[source]


# --- Chat mode -------------------------------------------------------------------


def _chat_mode(catalogue: Any) -> None:
    with st.sidebar:
        labels = {"%s  [%s]" % (s.name, _source_tag(s.source)): s.name for s in catalogue.specs}
        bot_name = labels[st.selectbox("Bot", list(labels.keys()))]
        spec = catalogue.get(bot_name)

        model_label = st.selectbox("Model", list(MODELS.keys()))
        model_id = MODELS[model_label]
        api_key = ""
        if model_id is not None:
            api_key = st.text_input(
                "Anthropic API key",
                value=os.environ.get("ANTHROPIC_API_KEY", ""),
                type="password",
                help="Session-only — never written to disk. Falls back to ANTHROPIC_API_KEY if set.",
            )

        st.divider()
        st.subheader("Rider")
        rider_mode = st.radio("Rider source", ["Preset rider", "Custom rider"], horizontal=True)
        oms_available = True
        if rider_mode == "Preset rider":
            scenarios = scenarios_for(spec.persona)
            scenario_label = st.selectbox("Scenario", [s.label for s in scenarios])
            scenario = next(s for s in scenarios if s.label == scenario_label)
            resolved = resolved_for_preset(scenario)
            channel = scenario.channel
            oms_available = scenario.oms_available
            rider_display = scenario.label
        else:
            if spec.persona == "dealer":
                resolved = custom_dealer_form()
                channel = "dealer_app"
            else:
                resolved = custom_customer_form()
                channel = "website_chat"
            rider_display = "Custom: %s" % (resolved.profile or {}).get("name", "unnamed")

        pill: Optional[str] = None
        if spec.topic and spec.persona == "customer":
            if st.checkbox("Arrive via the '%s' pill" % spec.topic, value=False):
                pill = spec.topic

        st.divider()
        st.caption("Tools this bot may call: " + ", ".join(spec.tools))

    # One Runtime per (bot, rider, model). Changing any of them starts a fresh
    # conversation; editing the prompt does not.
    session_key = (bot_name, rider_display, repr(resolved), model_id, bool(api_key))
    if st.session_state.get("session_key") != session_key or "session" not in st.session_state:
        if model_id is not None and not api_key:
            st.session_state["session"] = None
        else:
            st.session_state["session"] = _new_session(
                bot_name, resolved, channel, oms_available, _make_llm(model_id, api_key)
            )
        st.session_state["session_key"] = session_key
        st.session_state["transcript"] = []
    session: Optional[PlaygroundSession] = st.session_state["session"]

    prompt_key = "prompt_%s" % bot_name
    if prompt_key not in st.session_state:
        st.session_state[prompt_key] = spec.prompt

    col_prompt, col_chat = st.columns([1, 1])

    with col_prompt:
        st.subheader("System prompt (%s)" % bot_name)
        edited_prompt = st.text_area(
            "Applies from the next turn. Nothing is saved until you click a Save button.",
            value=st.session_state[prompt_key],
            height=480,
            key="textarea_%s" % bot_name,
        )
        st.session_state[prompt_key] = edited_prompt
        if session is not None and edited_prompt != spec.prompt and session.prompt_override != edited_prompt:
            session.set_prompt(edited_prompt)

        if spec.source == DRAFT:
            if st.button("Save prompt to draft"):
                try:
                    save_draft(dict(spec.to_dict(), prompt=edited_prompt), DRAFTS_DIR)
                    st.success("Draft updated.")
                    st.rerun()
                except BotSpecError as exc:
                    st.error(str(exc))
        else:
            if st.button("Save diff for review"):
                path = _save_prompt_diff(bot_name, spec.prompt, edited_prompt)
                st.success("Diff written to %s — review and apply it as a normal PR." % path)
                st.code(path.read_text(), language="diff")

        if session is not None:
            with st.expander("System prompt as the model will see it"):
                st.text(session.system_prompt_preview(pill))

    with col_chat:
        st.subheader("Test conversation")
        st.caption("Rider: %s · Channel: %s" % (rider_display, channel))
        if st.button("Reset conversation"):
            st.session_state.pop("session_key", None)
            st.rerun()

        if session is None:
            st.info("Enter an API key in the sidebar, or pick the offline planner, to start.")
            return

        for turn in st.session_state["transcript"]:
            with st.chat_message(turn["role"]):
                for name in turn.get("attachments", []):
                    st.caption("📎 %s" % name)
                st.write(turn["content"])
                if turn["role"] == "assistant":
                    st.caption(turn["summary"])
                    if turn["events"]:
                        with st.expander("Trace"):
                            st.json(turn["events"])

        uploaded_files = st.file_uploader(
            "📎 Attach image or PDF",
            type=["png", "jpg", "jpeg", "pdf"],
            accept_multiple_files=True,
            key="uploader_%s" % bot_name,
        )

        user_text = st.chat_input("Type a test message…")
        if user_text or uploaded_files:
            attachments = [_attachment_from_upload(f) for f in (uploaded_files or [])]
            text = (user_text or "").strip()
            st.session_state["transcript"].append(
                {"role": "user", "content": text or "(uploaded file)", "attachments": [f.name for f in uploaded_files or []]}
            )
            try:
                result = session.send(text, pill=pill, attachments=attachments)
                reply = result.reply
                summary = "handled_by=%s · tools=%s%s%s" % (
                    reply.handled_by,
                    ", ".join(reply.metadata.get("tool_calls", [])) or "none",
                    " · ESCALATED" if reply.escalated else "",
                    " · ticket %s" % reply.ticket_id if reply.ticket_id else "",
                )
                st.session_state["transcript"].append(
                    {"role": "assistant", "content": reply.text, "summary": summary, "events": result.events}
                )
            except Exception as exc:  # surfaced, not swallowed: this is a test bench
                st.session_state["transcript"].append(
                    {"role": "assistant", "content": "Error: %s" % exc, "summary": "error", "events": []}
                )
            st.rerun()


# --- New bot mode -------------------------------------------------------------


def _new_bot_mode(catalogue: Any) -> None:
    st.subheader("New bot")
    st.caption(
        "Saves a draft spec into %s. Drafts are testable in Chat immediately and are never "
        "loaded by the deployed API." % DRAFTS_DIR
    )

    persona = st.radio("Persona", ["customer", "dealer"], horizontal=True)
    name = st.text_input("Name (snake_case)", "brakes_support")
    topic = st.text_input("Topic (snake_case; the knowledge topic and the triage topic)", "brakes")
    keywords_text = st.text_area(
        "Keywords, one per line (English plus Hindi/Hinglish as customers actually type them)",
        "brake\nbraking\nब्रेक\nbrek",
        height=120,
    )
    tools = st.multiselect(
        "Tools", sorted(PERSONA_TOOLS[persona]),
        default=[t for t in ("lookup_warranty_record", "search_knowledge", "create_support_ticket") if t in PERSONA_TOOLS[persona]],
    )
    prompt = st.text_area("Prompt", prompt_template(persona, topic or "<topic>"), height=420)

    with st.expander("Knowledge records for this topic (optional)"):
        st.caption("One record per sub-issue, in the knowledge/README.md format. Symptoms and steps one per line.")
        record_count = st.number_input("Records", min_value=0, max_value=10, value=0, step=1)
        records: List[Dict[str, Any]] = []
        for i in range(int(record_count)):
            st.markdown("**Record %d**" % (i + 1))
            records.append(
                {
                    "id": st.text_input("id", "%s-%d" % (topic or "topic", i + 1), key="kb_id_%d" % i),
                    "title": st.text_input("title", "", key="kb_title_%d" % i),
                    "symptoms": [s.strip() for s in st.text_area("symptoms", "", key="kb_sym_%d" % i).splitlines() if s.strip()],
                    "steps": [s.strip() for s in st.text_area("steps", "", key="kb_steps_%d" % i).splitlines() if s.strip()],
                    "escalate_when": st.text_input("escalate_when", "", key="kb_esc_%d" % i),
                }
            )

    col_save, col_export, col_delete = st.columns(3)
    with col_save:
        if st.button("Save draft"):
            raw = {
                "name": name.strip(),
                "persona": persona,
                "topic": topic.strip() or None,
                "keywords": [k.strip() for k in keywords_text.splitlines() if k.strip()],
                "tools": tools,
                "prompt": prompt,
            }
            try:
                path = save_draft(raw, DRAFTS_DIR)
                written = save_draft_knowledge(topic.strip(), records, DRAFTS_DIR) if records and topic.strip() else []
                st.success("Draft saved to %s%s. Switch to Chat to test it." % (path, " (+%d knowledge records)" % len(written) if written else ""))
            except (BotSpecError, KnowledgeError) as exc:
                st.error(str(exc))
    with col_export:
        if st.button("Export for review"):
            try:
                paths = export_for_review(name.strip(), DRAFTS_DIR, EXPORT_DIR)
                st.success("Exported:\n" + "\n".join("- %s" % p for p in paths))
                st.caption("Move these into bots/ and knowledge/ in the repo and open a PR.")
            except (BotSpecError, KeyError) as exc:
                st.error("Save the draft first: %s" % exc)
    with col_delete:
        if st.button("Delete draft"):
            delete_draft(name.strip(), DRAFTS_DIR)
            st.session_state.pop("session_key", None)
            st.success("Deleted.")

    st.divider()
    st.subheader("Bots in the catalogue")
    rows = [
        {"name": s.name, "persona": s.persona, "topic": s.topic or "", "source": _source_tag(s.source),
         "keywords": ", ".join(s.keywords), "tools": ", ".join(s.tools)}
        for s in catalogue.specs
    ]
    st.dataframe(rows, use_container_width=True)


# --- main ---------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Emotorad AI — playground", layout="wide")
    st.title("Bot playground")

    try:
        catalogue = load_catalogue(DRAFTS_DIR)
    except BotSpecError as exc:
        st.error("A draft failed to load: %s\n\nFix or delete it under %s." % (exc, DRAFTS_DIR))
        return

    with st.sidebar:
        mode = st.radio("Mode", ["Chat", "New bot"], horizontal=True)
        st.divider()

    if mode == "Chat":
        _chat_mode(catalogue)
    else:
        _new_bot_mode(catalogue)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the test suite and a syntax check**

Run: `python3 -m unittest discover -s tests -t . && python3 -m py_compile src/emotorad_ai/playground.py src/emotorad_ai/playground_riders.py`
Expected: all tests pass, no compile errors.

- [ ] **Step 4: Verify the UI by hand**

Run from the repo root:

```bash
PYTHONPATH=src streamlit run src/emotorad_ai/playground.py --server.headless true --server.port 8501
```

Open http://127.0.0.1:8501 and check, in order:

1. Chat → bot `battery_support`, model "Offline planner", rider Ananya. Send "battery won't charge". Reply appears with caption `handled_by=battery_support · tools=search_knowledge`, Trace expander lists `routed` and `tool_call`.
2. Same, send "the battery is swelling". Caption shows `ESCALATED`, trace shows `guardrail`.
3. Rider Priya (3 bikes), send "battery won't charge". Reply asks which bike. Send "2". Routed to battery_support.
4. New bot → save the default brakes draft with one knowledge record (id `brakes-squeak`, symptoms `squeak`/`squeal`, steps one line). Success message.
5. Chat → bot `brakes_support [draft]`, offline planner, Ananya. Send "my brake squeaks". `handled_by=brakes_support`, `tools=search_knowledge`, reply contains the step you wrote.
6. Edit the prompt in the left pane, send another message, open "System prompt as the model will see it": it starts with the edited text. Click "Save prompt to draft".
7. New bot → "Export for review" → files listed under `.playground/export/`.
8. New bot → "Delete draft"; back in Chat the draft is gone.
9. With an API key and Sonnet 5, repeat step 1: a real reply, trace shows `llm_turn`.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/playground.py src/emotorad_ai/playground_riders.py
git commit -m "feat(playground): chat on the real runtime, New bot mode for draft specs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Ship `bots/`, mount drafts on staging, document

**Files:**
- Modify: `Dockerfile`, `.github/workflows/deploy-staging.yml`, `README.md`, `CLAUDE.md`

- [ ] **Step 1: Dockerfile**

After `COPY knowledge/ knowledge/` add:

```dockerfile
COPY bots/ bots/
```

- [ ] **Step 2: Deploy workflow**

In the "Package deploy artifact" step, change the tar command's file list to end with `src knowledge bots`:

```yaml
          tar --exclude='.git' --exclude='tests' --exclude='docs' --exclude='logs' \
              --exclude='.playground' --exclude='requirements-dev.txt' --exclude='__pycache__' \
              -czf app.tar.gz Dockerfile docker requirements.txt src knowledge bots
```

In the SSM command list, add a `mkdir` before the docker run and mount the directory. The commands array becomes:

```yaml
            --parameters commands="[
              \"cd /opt/emotorad-ai\",
              \"mkdir -p /opt/emotorad-ai/playground\",
              \"aws s3 cp s3://$S3_BUCKET/deploy/app.tar.gz app.tar.gz --region $AWS_REGION\",
              \"tar xzf app.tar.gz\",
              \"docker build -t emotorad-ai:stage .\",
              \"docker rm -f emotorad-ai || true\",
              \"docker run -d --name emotorad-ai --restart unless-stopped --log-driver=awslogs --log-opt awslogs-region=$AWS_REGION --log-opt awslogs-group=emotorad-ai-stage --log-opt awslogs-create-group=true -p 127.0.0.1:8000:8000 -v /opt/emotorad-ai/playground:/app/.playground -e EMOTORAD_AI_BOT_DRAFTS=/app/.playground/drafts -e EMOTORAD_AI_MODE=offline -e EMOTORAD_AI_PLAYGROUND_USER='${{ secrets.PLAYGROUND_BASIC_AUTH_USER }}' -e EMOTORAD_AI_PLAYGROUND_PASSWORD='${{ secrets.PLAYGROUND_BASIC_AUTH_PASSWORD }}' emotorad-ai:stage\"
            ]" \
```

Add a comment above the step:

```yaml
      # /opt/emotorad-ai/playground is bind-mounted so playground drafts and
      # exports survive a redeploy — the tarball excludes .playground and the
      # container is rebuilt every time. The deployed API never loads drafts;
      # only the playground does (EMOTORAD_AI_BOT_DRAFTS).
```

- [ ] **Step 3: README module map and a bots section**

In `README.md`, in the module table replace the `router.py` row with:

```markdown
| `triage.py` | §3.5 | Conversational triage: bike selection, keyword classification, hand-off |
| `bots.py` | — | The bot catalogue: built-ins plus `bots/*.yaml`, validated at load |
| `agents/generic.py` | — | A sub-agent from a spec, with the shared blocks from `agents/blocks.py` |
| `playground_runtime.py` | — | The playground's `Runtime` per session, with a trace |
```

After the module table add:

```markdown
## Adding a bot

A bot is a YAML file (see `bots/README.md`): name, persona, topic, triage
keywords, a tool slice from the registry, and a prompt. Draft one in the
playground ("New bot"), test it in Chat — every turn runs this runtime, guardrails
included — then "Export for review" and move the files into `bots/` and
`knowledge/` in a PR. Persona tool allowlists are in `bots.py` and cannot be
widened from a spec.
```

- [ ] **Step 4: CLAUDE.md**

In `CLAUDE.md` under "The code so far", append:

```markdown
**Bot catalogue (2026-09):** sub-agents are entries in `bots.py`'s catalogue —
the four Python agents plus `bots/*.yaml`. Runtime, triage keywords, the
`search_knowledge` topic enum and the "I can help with …" reply derive from it.
The playground's Chat mode runs the real `Runtime` (offline planner or the
Anthropic API via `AnthropicClaude`; production stays on Bedrock) and its New
bot mode writes drafts under `EMOTORAD_AI_BOT_DRAFTS`. Design:
`docs/superpowers/specs/2026-09-05-custom-bot-builder-design.md`.
```

- [ ] **Step 5: Run the suite one last time**

Run: `python3 -m unittest discover -s tests -t .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .github/workflows/deploy-staging.yml README.md CLAUDE.md
git commit -m "chore(deploy): ship bots/, mount playground drafts on staging, document

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** §2 spec + validation → Task 4. §3 blocks/generic/catalogue/runtime/triage/mocks → Tasks 1–5. §4 builder → Tasks 9–10. §5 test flow (`AnthropicClaude`, `StaticResolver`, session, trace, attachments) → Tasks 6–8, 10. §6 persistence/deploy → Tasks 9 (paths) and 11. §7 tests → each task; `test_triage.py`, `test_motor_support.py`, `test_dealer_orders.py` untouched. §8 out of scope: nothing here adds tools, guardrails or roles.

**Type consistency.** `BotCatalogue.load(published_dir, drafts_dir, extra)` in Task 4 is what Tasks 8–9 call. `PlaygroundSession(bot, resolved, channel, catalogue, llm, oms_available, today, drafts_dir)` in Task 8 matches Task 10's `_new_session`. `TriageAgent(topic_agents, keywords, supported_summary)` in Task 2 matches Task 5. `build_registry(topics=, knowledge_base=, knowledge_bike=)` in Task 3 matches Task 8. `BotSpec.module` is set in `builtin_specs()` and read in `PlaygroundSession._patched_builtin_prompt`.

**Known judgement calls.** The drafts root is `.playground/drafts` (holding `bots/` and `knowledge/`); the spec line was corrected in Task 9. Haiku 4.5 is dropped from the playground model list because the runtime always sends adaptive thinking and `output_config.effort`, which that model rejects.
