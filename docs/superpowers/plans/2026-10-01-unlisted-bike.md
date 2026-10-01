# Unlisted Bike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the customer's bike is not in the list the bot shows, the bot asks for its frame number and model, then carries on with the issue; "its not one of these 2" never picks bike 2.

**Architecture:** Triage checks for a "not in the list" reply before matching any bike, then collects the frame number and model in a new phase and records them on the conversation (`unlisted_bike`). The runtime treats that as the conversation's bike: it tells the agent, blocks warranty claims about it, and lets the ticket tool put its frame number on a ticket.

**Tech Stack:** Python 3.12, unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-unlisted-bike-design.md`

## Global Constraints

- `ASK_FOR_UNLISTED_BIKE = "No problem. Please send your bike's frame number and model. The frame number is printed on the sticker on the frame."`
- `ASK_FOR_MODEL = "Thanks. Which model is it?"`
- `ASK_FOR_FRAME = "Thanks. What's its frame number? It's printed on the sticker on the frame."`
- With no issue yet: `"Thanks: <label>. What is happening with the bike? A short description is enough."`, where the label is `"<model>, frame <frame>"` (either part left out when unknown; `"your bike"` when both are).
- The agent's context line: `"The customer's bike for this conversation is not registered on their number: <label>, as they read it. There is no warranty record for it: never say it is or is not covered."`, plus `" Use this frame number on any ticket."` when the frame number is known.
- `UNLISTED_SOURCE = "given by the customer; not registered on this number"`
- `UNLISTED_MAX_ASKS = 2`: two asks in the collecting step, the opening one included, so each missing part is asked for at most twice; then the bot carries on with what it has.
- The "not in the list" check runs before `match_bike`, always.
- Frame numbers of listed bikes are resolved by the ticket tool as today.
- British English, no em dashes in new code comments and texts. Files are LF (write with Python `write_bytes`, or normalise after the Write tool).
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`; copy `suite.py` from the session scratchpad).

## Review Focus

1. During collecting, the customer sends the frame number of a bike that **is** in the list ("oh, it's EMXP2025004990 after all"): that listed bike is chosen, not stored as an unlisted one (Task 1 `test_a_listed_frame_while_collecting_chooses_that_bike`).
2. A frame number typed with a space or in lower case ("emxp 2026009999"): read as `EMXP2026009999` (Task 1 `test_a_frame_with_a_space_or_in_lower_case`).
3. During collecting, the customer describes the problem instead ("my battery isn't charging"): the issue is kept for later, it is not taken as the model, and the bot asks again for what is missing (Task 1 `test_the_issue_said_while_collecting_is_kept_not_taken_as_the_model`).
4. During collecting, a question or "I don't know" ("where do I find the model?"): never taken as the model (Task 1 `test_a_question_or_dont_know_is_never_the_model`).
5. A safety report after the unlisted bike is recorded: the safety ticket carries the bike's frame number (Task 2 `test_a_safety_ticket_carries_the_unlisted_frame`).

## Spec clarifications

- "Each missing part is asked for at most twice" is implemented as two asks in the collecting step, the opening one included (`UNLISTED_MAX_ASKS = 2`). The opening question asks for both parts, so every part is asked for at most twice.
- The label stored with `select_bike` is the same `"<model>, frame <frame>"` label the customer sees, rather than the spec's `"<model> (frame <frame>)"`: one format everywhere.

---

### Task 1: Triage collects a bike that is not in the list

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`AWAITING_UNLISTED_BIKE`; `ConversationState.unlisted_bike`, `unlisted_asks`)
- Modify: `src/emotorad_ai/triage.py` (the check, the collecting step, the helpers; `TriageAgent` loses `unlisted_agent`)
- Modify: `src/emotorad_ai/runtime.py` (`TriageAgent(TOPIC_AGENTS)`; the dead late-registration branch after triage; the pinned-agent guard in `_node_prepare`)
- Modify: `tests/test_triage.py` (three tests that expected late registration), `tests/test_amigo_bikes_without_frame.py` (the constructor call)
- Test: `tests/test_unlisted_bike.py` (new)

**Interfaces:**
- Produces: in `conversation.py`, `AWAITING_UNLISTED_BIKE = "awaiting_unlisted_bike"`, `ConversationState.unlisted_bike: Optional[Dict[str, Optional[str]]]`, `ConversationState.unlisted_asks: int`; in `triage.py`, `not_listed(text) -> bool`, `known_model(text) -> Optional[str]`, `unlisted_label(bike) -> str`, `unlisted_context(bike) -> str`, `unlisted_as_bike(bike) -> Dict[str, Any]`, constants `ASK_FOR_UNLISTED_BIKE`, `ASK_FOR_MODEL`, `ASK_FOR_FRAME`, `UNLISTED_MAX_ASKS`, `UNLISTED_REF = "unlisted"`; `TriageAgent(topic_agents)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_unlisted_bike.py`:

```python
"""A bike that is not in the list (spec 2026-10-01-unlisted-bike-design.md)."""

import unittest

from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, AWAITING_ISSUE, AWAITING_UNLISTED_BIKE, ConversationState
from emotorad_ai.triage import (
    ASK_FOR_FRAME,
    ASK_FOR_MODEL,
    ASK_FOR_UNLISTED_BIKE,
    TriageAgent,
    known_model,
    not_listed,
    unlisted_as_bike,
    unlisted_context,
    unlisted_label,
)
from tests.test_triage import BIKES, TOPIC_AGENTS, message, resolved

TWO = BIKES[:2]


def choosing(topic="battery"):
    state = ConversationState("c1")
    state.move_to(AWAITING_BIKE_SELECTION, "verified")
    state.pending_topic = topic
    return state


class NotListedTests(unittest.TestCase):
    def test_the_phrases(self):
        for text in ("its not one of these 2", "none of these", "None", "neither", "it's a different bike",
                     "not mine", "not in the list", "not these", "another bike", "koi nahi", "dono nahi",
                     "इनमें से कोई नहीं"):
            self.assertTrue(not_listed(text), text)

    def test_a_choice_is_not(self):
        for text in ("2", "the second one", "the Doodle", "EMXP2025004990", "yes",
                     "not the first, the second"):
            self.assertFalse(not_listed(text), text)


class KnownModelTests(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(known_model("trex air"), "T-Rex Air")
        self.assertEqual(known_model("its a T-Rex Plus V2"), "T-Rex Plus V2")
        self.assertEqual(known_model("EMX plus"), "EMX Plus")
        self.assertEqual(known_model("doodle v3"), "Doodle V3")

    def test_a_frame_number_or_nothing_is_no_model(self):
        self.assertIsNone(known_model("TREX2024881201"))
        self.assertIsNone(known_model("no idea"))


class LabelTests(unittest.TestCase):
    def test_labels_and_context(self):
        bike = {"frame_number": "EMXP2026009999", "model": "T-Rex Air"}
        self.assertEqual(unlisted_label(bike), "T-Rex Air, frame EMXP2026009999")
        self.assertEqual(unlisted_label({"frame_number": None, "model": "T-Rex Air"}), "T-Rex Air")
        self.assertEqual(unlisted_label({"frame_number": None, "model": None}), "your bike")
        self.assertIn("not registered on their number: T-Rex Air, frame EMXP2026009999, as they read it",
                      unlisted_context(bike))
        self.assertIn("Use this frame number on any ticket.", unlisted_context(bike))
        self.assertNotIn("Use this frame number", unlisted_context({"frame_number": None, "model": "T-Rex Air"}))
        self.assertEqual(unlisted_context(None), "")
        self.assertEqual(unlisted_as_bike(bike),
                         {"product_name": "T-Rex Air", "frame_number": "EMXP2026009999", "on_record": False})


class CollectingTests(unittest.TestCase):
    def setUp(self):
        self.triage = TriageAgent(TOPIC_AGENTS)

    def say(self, state, text, bikes=TWO):
        return self.triage.handle(message(text), resolved(bikes), state)

    def test_its_not_one_of_these_2_picks_no_bike(self):
        state = choosing()
        outcome = self.say(state, "its not one of these 2")
        self.assertEqual(outcome.reply, ASK_FOR_UNLISTED_BIKE)
        self.assertIsNone(state.selected_frame)
        self.assertEqual(state.phase, AWAITING_UNLISTED_BIKE)

    def test_frame_and_model_in_one_reply_carry_on_to_the_kept_issue(self):
        state = choosing()
        self.say(state, "none of these")
        outcome = self.say(state, "EMXP2026009999, T-Rex Air")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.unlisted_bike, {"frame_number": "EMXP2026009999", "model": "T-Rex Air"})
        self.assertEqual(state.selected_frame, "EMXP2026009999")

    def test_frame_then_model(self):
        state = choosing()
        self.say(state, "neither")
        self.assertEqual(self.say(state, "EMXP2026009999").reply, ASK_FOR_MODEL)
        outcome = self.say(state, "trex air")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.unlisted_bike["model"], "T-Rex Air")

    def test_model_then_frame(self):
        state = choosing()
        self.say(state, "not one of these")
        self.assertEqual(self.say(state, "It's a T-Rex Air").reply, ASK_FOR_FRAME)
        self.assertEqual(self.say(state, "EMXP2026009999").agent, "battery_support")

    def test_the_whole_answer_in_the_first_reply(self):
        state = choosing()
        outcome = self.say(state, "not these, mine is a T-Rex Air EMXP2026009999")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.unlisted_bike, {"frame_number": "EMXP2026009999", "model": "T-Rex Air"})

    def test_a_model_we_do_not_know_is_kept_as_typed(self):
        state = choosing()
        self.say(state, "DDL32023045678")  # a frame number that is not in the list
        self.say(state, "Lil E")
        self.assertEqual(state.unlisted_bike["model"], "Lil E")

    def test_it_carries_on_after_two_asks(self):
        state = choosing()
        self.say(state, "none of these")          # ask 1: both
        self.say(state, "T-Rex Air")              # ask 2: the frame
        outcome = self.say(state, "I can't find it")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.unlisted_bike, {"frame_number": None, "model": "T-Rex Air"})
        self.assertEqual(state.selected_frame, "unlisted")

    def test_with_no_issue_yet_it_confirms_and_asks_for_it(self):
        state = choosing(topic=None)
        self.say(state, "none of these")
        outcome = self.say(state, "EMXP2026009999 T-Rex Air")
        self.assertEqual(outcome.reply, "Thanks: T-Rex Air, frame EMXP2026009999. What is happening with the bike? "
                                        "A short description is enough.")
        self.assertEqual(state.phase, AWAITING_ISSUE)

    def test_a_frame_number_not_in_a_two_bike_list(self):
        state = choosing()
        self.assertEqual(self.say(state, "DDL32023045678").reply, ASK_FOR_MODEL)
        self.assertEqual(state.unlisted_bike["frame_number"], "DDL32023045678")

    def test_a_listed_choice_still_works(self):
        state = choosing()
        self.say(state, "2")
        self.assertEqual(state.selected_frame, TWO[1]["frame_number"])
        self.assertIsNone(state.unlisted_bike)

    def test_one_bike_no_asks_for_the_bike(self):
        state = choosing()
        self.assertEqual(self.say(state, "no", bikes=BIKES[:1]).reply, ASK_FOR_UNLISTED_BIKE)
        self.assertIsNone(state.selected_frame)

    def test_a_listed_frame_while_collecting_chooses_that_bike(self):
        state = choosing()
        self.say(state, "none of these")
        outcome = self.say(state, "oh, it's EMXP2025004990 after all")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "EMXP2025004990")
        self.assertIsNone(state.unlisted_bike)

    def test_a_frame_with_a_space_or_in_lower_case(self):
        state = choosing()
        self.say(state, "none of these")
        self.say(state, "emxp 2026009999")
        self.assertEqual(state.unlisted_bike["frame_number"], "EMXP2026009999")

    def test_the_issue_said_while_collecting_is_kept_not_taken_as_the_model(self):
        state = choosing(topic=None)
        self.say(state, "DDL32023045678")
        outcome = self.say(state, "my battery isn't charging")
        self.assertEqual(outcome.reply, ASK_FOR_MODEL)
        self.assertIsNone(state.unlisted_bike["model"])
        self.assertEqual(state.pending_topic, "battery")

    def test_a_question_or_dont_know_is_never_the_model(self):
        for text in ("where do I find the model?", "I don't know", "pata nahi"):
            state = choosing()
            self.say(state, "DDL32023045678")
            self.say(state, text)
            self.assertIsNone(state.unlisted_bike["model"], text)
```

In `tests/test_triage.py`, `OneBikeSelectionTests`:

- `setUp`: `self.triage = TriageAgent(TOPIC_AGENTS)`.
- Replace `test_no_goes_to_the_unlisted_agent`, `test_ji_nahi_goes_to_the_unlisted_agent` and `test_the_frame_number_of_a_bike_not_listed_goes_to_the_unlisted_agent` with:

```python
    def test_no_asks_for_the_frame_number_and_model(self):
        # The bike not listed (spec 2026-10-01, unlisted bike): no route to
        # late registration any more.
        outcome = self.triage.handle(message("no"), resolved(BIKES[:1]), self.state)
        self.assertFalse(outcome.is_handoff)
        self.assertEqual(outcome.reply, ASK_FOR_UNLISTED_BIKE)
        self.assertEqual(self.state.phase, AWAITING_UNLISTED_BIKE)
        self.assertIsNone(self.state.selected_frame)

    def test_ji_nahi_asks_for_the_frame_number_and_model(self):
        outcome = self.triage.handle(message("ji nahi"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.reply, ASK_FOR_UNLISTED_BIKE)
        self.assertIsNone(self.state.selected_frame)

    def test_the_frame_number_of_a_bike_not_listed_asks_for_its_model(self):
        # The question invites it ("send the frame number of the bike you
        # mean"); it used to loop on "did not catch" for ever.
        outcome = self.triage.handle(message("DDL32023045678"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.reply, ASK_FOR_MODEL)
        self.assertEqual(self.state.unlisted_bike["frame_number"], "DDL32023045678")
```

and add `AWAITING_UNLISTED_BIKE` to its `emotorad_ai.conversation` import and `ASK_FOR_MODEL, ASK_FOR_UNLISTED_BIKE` to its `emotorad_ai.triage` import.

In `tests/test_amigo_bikes_without_frame.py`, `TriageAgent({"battery": "battery_support"}, unlisted_agent="late")` becomes `TriageAgent({"battery": "battery_support"})`.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." PYTHONIOENCODING=utf-8 python -m unittest tests.test_unlisted_bike tests.test_triage tests.test_amigo_bikes_without_frame`
Expected: ERROR, `ImportError: cannot import name 'AWAITING_UNLISTED_BIKE'` (and in `test_amigo_bikes_without_frame` nothing yet, since the old constructor still takes the keyword: it passes).

- [ ] **Step 3: The state**

In `src/emotorad_ai/conversation.py`, after `AWAITING_ISSUE = "awaiting_issue"`:

```python
# The customer said their bike is not in the list: collecting its frame number
# and model (spec 2026-10-01, unlisted bike).
AWAITING_UNLISTED_BIKE = "awaiting_unlisted_bike"
```

In `ConversationState`, after `selected_bike_label: Optional[str] = None`:

```python
    # A bike the customer says is not in the list (spec 2026-10-01, unlisted
    # bike): {"frame_number", "model"} as they gave them, either None if they
    # never did, and the asks made while collecting them.
    unlisted_bike: Optional[Dict[str, Optional[str]]] = None
    unlisted_asks: int = 0
```

- [ ] **Step 4: Triage**

In `src/emotorad_ai/triage.py`:

Imports: add `AWAITING_UNLISTED_BIKE` to the `from .conversation import (...)` list, and after the existing imports:

```python
from .tools import fixtures
from .tools.amigo import MODEL_NAMES
```

After `def names_a_frame(...)`, add:

```python
# --- a bike that is not in the list (spec 2026-10-01, unlisted bike) ---------
# "its not one of these 2" chose bike 2 on staging: the ordinal matched and
# nothing checked for the "not". This is checked before any bike is matched.
_NOT_LISTED = re.compile(
    r"\b(?:not|none)\s+(?:(?:one|any)\s+)?of\s+(?:these|them|those|the\s+two|the\s+three|both)\b"
    r"|\bneither\b|^\W*none\W*$|\bnot\s+these\b"
    r"|\bnot\s+(?:mine|listed|here|there|in\s+(?:the|this|your)\s+list|on\s+(?:the|this|your)\s+list)\b"
    r"|\b(?:a\s+)?different\s+(?:one|bike|cycle)\b|\banother\s+(?:bike|cycle)\b"
    r"|\bnot\s+(?:this|that)\s+(?:one|bike)\b"
    r"|\bkoi\s+(?:bhi\s+)?nahi\b|\bdono\s+(?:hi\s+)?nahi\b|इनमें\s+से\s+कोई\s+नहीं|दोनों\s+नहीं",
    re.IGNORECASE,
)
_DONT_KNOW = re.compile(
    r"\b(?:don'?t|do\s+not)\s+know\b|\bnot\s+sure\b|\bno\s+idea\b|\bpata\s+nahi\b|\bcan'?t\s+find\b|पता\s+नहीं",
    re.IGNORECASE,
)
# EMotorad's model names: the Amiigo app's, and those on the test records.
KNOWN_MODELS = tuple(sorted(
    set(MODEL_NAMES.values())
    | {record["product_name"] for records in fixtures.WARRANTY_RECORDS.values() for record in records
       if record.get("product_name")},
    key=len, reverse=True,
))
_MODEL_PATTERNS = [
    (model, re.compile(r"\b" + r"\W*".join(re.findall(r"[a-z0-9]+", model.lower())) + r"\b", re.IGNORECASE))
    for model in KNOWN_MODELS
]

ASK_FOR_UNLISTED_BIKE = ("No problem. Please send your bike's frame number and model. "
                         "The frame number is printed on the sticker on the frame.")
ASK_FOR_MODEL = "Thanks. Which model is it?"
ASK_FOR_FRAME = "Thanks. What's its frame number? It's printed on the sticker on the frame."
# Two asks while collecting, the opening one included: each missing part is
# asked for at most twice, then the bot carries on with what it has.
UNLISTED_MAX_ASKS = 2
# The conversation's bike reference when the customer never gave a frame number.
UNLISTED_REF = "unlisted"


def not_listed(text: str) -> bool:
    return bool(_NOT_LISTED.search(text or ""))


def _joined(text: str) -> str:
    """A frame number typed with spaces or hyphens before its digits, joined:
    "emxp 2026009999" is EMXP2026009999."""
    return re.sub(r"(?<=[A-Za-z0-9])[\s-]+(?=\d)", "", text or "")


def _find_frame(text: str) -> Optional[str]:
    match = _FRAME.search(_joined(text))
    return match.group(0).upper() if match else None


def known_model(text: str) -> Optional[str]:
    """An EMotorad model named in the text, the longest when several match
    ("T-Rex Plus V2" over "T-Rex Plus"). Frame numbers are left out first:
    TREX2024881201 is not a T-Rex, and it holds "X2"."""
    words = _FRAME.sub(" ", _joined(text))
    for model, pattern in _MODEL_PATTERNS:
        if pattern.search(words):
            return model
    return None


def unlisted_label(bike: Optional[Dict[str, Optional[str]]]) -> str:
    bike = bike or {}
    parts = [bike.get("model") or "", "frame %s" % bike["frame_number"] if bike.get("frame_number") else ""]
    return ", ".join(part for part in parts if part) or "your bike"


def unlisted_context(bike: Optional[Dict[str, Optional[str]]]) -> str:
    """What the agent is told about the conversation's unlisted bike, or ""."""
    if not bike:
        return ""
    text = ("The customer's bike for this conversation is not registered on their number: %s, as they read it. "
            "There is no warranty record for it: never say it is or is not covered." % unlisted_label(bike))
    if bike.get("frame_number"):
        text += " Use this frame number on any ticket."
    return "\n\n" + text


def unlisted_as_bike(bike: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """The unlisted bike in the shape a listed one has, for the knowledge
    filter and Jev."""
    return {"product_name": bike.get("model"), "frame_number": bike.get("frame_number"), "on_record": False}


def _listed_frame(text: str, bikes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A listed bike whose frame number the text gives exactly."""
    frame = _find_frame(text)
    for bike in bikes:
        if frame and frame == (bike.get("frame_number") or "").upper():
            return bike
    return None


def _model_as_typed(text: str, bike: Dict[str, Optional[str]], frame_in_reply: Optional[str]) -> bool:
    """Whether a reply is the model, as typed, when only the model is missing:
    a short answer that is not a question, not the issue, not "I don't know"."""
    words = (text or "").split()
    return bool(bike.get("frame_number") and not bike.get("model") and not frame_in_reply and words
                and len(words) <= 4 and not text.strip().endswith("?") and classify_issue(text) is None
                and not _DONT_KNOW.search(text) and not not_listed(text))
```

`TriageAgent.__init__`: remove the `unlisted_agent` parameter and the `self.unlisted_agent` lines with their comment; it becomes:

```python
    def __init__(self, topic_agents: Dict[str, str]) -> None:
        # topic -> sub-agent name, e.g. {"battery": "battery_support"}.
        self.topic_agents = topic_agents
```

`handle`: before `if state.phase == AWAITING_BIKE_SELECTION:` add

```python
        if state.phase == AWAITING_UNLISTED_BIKE:
            return self._collect_unlisted(text, resolved, state)
```

`_resolve_selection`: replace from `single = len(bikes) == 1` down to (not including) `state.select_bike(bike_ref(bike), bike_name(bike))` with:

```python
        single = len(bikes) == 1
        # Before any ordinal, frame number or model is matched (spec
        # 2026-10-01, unlisted bike): "its not one of these 2" chose bike 2.
        if not_listed(text) or (single and says_no(text)):
            return self._start_unlisted(text, resolved, state)
        bike = bikes[0] if single and says_yes(text) else match_bike(text, bikes)
        if bike is None and single and not bikes[0].get("frame_number") and names_a_frame(text):
            # The only bike has no frame number on record, so a frame number
            # typed now is the rider reading theirs, not a different bike.
            bike = bikes[0]
        if bike is None:
            if names_a_frame(text):
                # The frame number of a bike that is not in the list, as the
                # question invites ("send the frame number of the bike you mean").
                return self._start_unlisted(text, resolved, state)
            # Re-ask rather than guess. An unmatched reply usually means the
            # customer answered something else entirely, and picking a bike here
            # would silently attach the whole conversation to the wrong one.
            return TriageOutcome(
                reply="Sorry, I did not catch which bike you meant. " + which_bike_text(bikes),
                reason="selection_unmatched",
            )

```

After `_resolve_selection`, add:

```python
    def _start_unlisted(self, text: str, resolved: ResolvedIdentity, state: ConversationState) -> TriageOutcome:
        state.move_to(AWAITING_UNLISTED_BIKE, "bike_not_listed")
        state.unlisted_bike = {"frame_number": None, "model": None}
        state.unlisted_asks = 0
        return self._collect_unlisted(text, resolved, state)

    def _collect_unlisted(self, text: str, resolved: ResolvedIdentity, state: ConversationState) -> TriageOutcome:
        """The frame number and model of a bike that is not in the list, then
        on with the issue (spec 2026-10-01, unlisted bike)."""
        listed = _listed_frame(text, resolved.bikes)
        if listed is not None:
            # A listed bike's own frame number: it was in the list after all.
            state.unlisted_bike = None
            state.select_bike(bike_ref(listed), bike_name(listed))
            state.move_to(AWAITING_ISSUE, "bike_selected")
            topic, source = self._take_pending(state)
            return self._route_or_ask(topic, state, source)
        bike = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        frame = _find_frame(text)
        if frame and not bike.get("frame_number"):
            bike["frame_number"] = frame
        model = known_model(text)
        if model is None and _model_as_typed(text, bike, frame):
            model = " ".join(text.split())[:60]
        if model and not bike.get("model"):
            bike["model"] = model
        # The issue, if they gave it here, is kept for when the bike is known.
        topic = classify_issue(text)
        if topic and not state.pending_topic:
            state.pending_topic, state.pending_topic_source = topic, "text"
        state.unlisted_bike = bike
        missing = [part for part in ("frame_number", "model") if not bike.get(part)]
        if missing and state.unlisted_asks < UNLISTED_MAX_ASKS:
            state.unlisted_asks += 1
            reply = ASK_FOR_UNLISTED_BIKE if len(missing) == 2 else ASK_FOR_MODEL if missing == ["model"] else ASK_FOR_FRAME
            return TriageOutcome(reply=reply, reason="unlisted_bike:ask:%s" % "+".join(missing))
        return self._unlisted_done(state)

    def _unlisted_done(self, state: ConversationState) -> TriageOutcome:
        bike = state.unlisted_bike or {}
        state.select_bike(bike.get("frame_number") or UNLISTED_REF, unlisted_label(bike))
        state.move_to(AWAITING_ISSUE, "unlisted_bike")
        topic, source = self._take_pending(state)
        outcome = self._route_or_ask(topic, state, source)
        if outcome.reason == "issue_unknown":
            return TriageOutcome(reply="Thanks: %s. %s" % (unlisted_label(bike), outcome.reply), reason=outcome.reason)
        return outcome
```

- [ ] **Step 5: The runtime's triage wiring**

In `src/emotorad_ai/runtime.py`:

- `TriageAgent(TOPIC_AGENTS, unlisted_agent=LATE_WARRANTY)` becomes `TriageAgent(TOPIC_AGENTS)`.
- In the triage node, remove the branch that follows `if not outcome.is_handoff: return ...`:

```python
            if outcome.agent == LATE_WARRANTY and LATE_WARRANTY in self.agents:
                # The one bike listed is not theirs: straight to registration,
                # with no Jev category to route around it.
                return {"reply": self._run_agent_or_handover(LATE_WARRANTY, message, resolved, state)}
```

- In `_node_prepare`, `choosing = self.verify_gate is not None and state.phase == AWAITING_BIKE_SELECTION` becomes `choosing = self.verify_gate is not None and state.phase in (AWAITING_BIKE_SELECTION, AWAITING_UNLISTED_BIKE)`, and `AWAITING_UNLISTED_BIKE` joins the runtime's `from .conversation import ...`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." PYTHONIOENCODING=utf-8 python -m unittest tests.test_unlisted_bike tests.test_triage tests.test_amigo_bikes_without_frame tests.test_verify_first`
Expected: OK.

- [ ] **Step 7: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK. `grep -rn "unlisted_agent\|bike_not_listed" src tests` prints only the new `move_to(..., "bike_not_listed")` transition.

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/triage.py src/emotorad_ai/runtime.py tests/test_unlisted_bike.py tests/test_triage.py tests/test_amigo_bikes_without_frame.py
git commit -m "fix: a bike not in the list is asked for, never chosen from the list"
```

---

### Task 2: The rest of the conversation uses the unlisted bike

**Files:**
- Modify: `src/emotorad_ai/runtime.py` (`_selected_bike`; the agent's context and facts in `_run`; `_post_checks`; `_raise_safety_ticket`)
- Modify: `src/emotorad_ai/guardrails.py` (`claims_coverage`)
- Modify: `src/emotorad_ai/tools/mocks.py` (`UNLISTED_SOURCE`, `_unlisted_ticket_bike`, `create_support_ticket`)
- Test: `tests/test_unlisted_bike_runtime.py` (new)

**Interfaces:**
- Consumes: Task 1's `ConversationState.unlisted_bike`, `unlisted_as_bike`, `unlisted_context`, `ASK_FOR_UNLISTED_BIKE`.
- Produces: `guardrails.claims_coverage(reply: str) -> bool`; `mocks.UNLISTED_SOURCE`; the injected fact `unlisted_bike` for `create_support_ticket`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_unlisted_bike_runtime.py`:

```python
"""The unlisted bike through the rest of the conversation
(spec 2026-10-01-unlisted-bike-design.md)."""

import unittest
from datetime import date

from emotorad_ai.llm import call_tool, say
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, LOOKUP_WARRANTY_RECORD, UNLISTED_SOURCE, build_registry
from emotorad_ai.tools.registry import ToolContext
from emotorad_ai.triage import ASK_FOR_UNLISTED_BIKE
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

BIKE = {"frame_number": "EMXP2026009999", "model": "T-Rex Air"}


def unlisted(chat, phone=RIDER, not_mine="its not one of these 2"):
    chat.verify(phone, first="hi")
    assert ASK_FOR_UNLISTED_BIKE in chat.say(not_mine).text
    return chat.say("EMXP2026009999, T-Rex Air")


class StagingReplayTests(unittest.TestCase):
    def test_the_staging_chat(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        confirmed = unlisted(chat)
        self.assertIn("Thanks: T-Rex Air, frame EMXP2026009999. What is happening with the bike?", confirmed.text)
        reply = chat.say("battery isnt charging")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertNotIn("Which bike", reply.text)
        self.assertEqual(chat.state().unlisted_bike, BIKE)
        self.assertIn("not registered on their number: T-Rex Air, frame EMXP2026009999, as they read it",
                      chat.llm.requests[-1]["system"])

    def test_one_bike_listed_and_no(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        unlisted(chat, phone=ONE_BIKE, not_mine="no")
        self.assertEqual(chat.say("battery isnt charging").handled_by, "battery_support")
        self.assertEqual(chat.state().unlisted_bike, BIKE)


class CoverageTests(unittest.TestCase):
    def test_a_coverage_claim_about_the_unlisted_bike_is_blocked(self):
        # Ananya's listed EMX Plus is covered; the bike she is asking about is
        # not on her number, so the lookup says nothing about it.
        chat = Chat(replies=[call_tool(LOOKUP_WARRANTY_RECORD, {}, "toolu_1"),
                             say("Good news, it's covered under warranty.")])
        unlisted(chat, phone=ONE_BIKE, not_mine="no")
        reply = chat.say("battery isnt charging, is it under warranty?")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertNotIn("covered under warranty", reply.text)


class TicketTests(unittest.TestCase):
    def ticket(self, arguments, late):
        registry = build_registry(today=date(2026, 10, 1))
        envelope = registry.call(CREATE_SUPPORT_TICKET, dict({
            "category": "battery_charging", "description": "Not charging.", "severity": "normal",
            "idempotency_key": "k1"}, **arguments),
            ToolContext(conversation_id="c1", phone=RIDER, late=dict({"evidence_seen": lambda: True}, **late)))
        return registry.tickets.tickets[envelope["data"]["ticket_id"]]

    def test_a_ticket_for_the_unlisted_bike_carries_its_frame(self):
        ticket = self.ticket({}, {"unlisted_bike": lambda: dict(BIKE)})
        self.assertEqual((ticket["frame_number"], ticket["bike_model"], ticket["frame_number_source"]),
                         ("EMXP2026009999", "T-Rex Air", UNLISTED_SOURCE))

    def test_naming_the_unlisted_frame_is_accepted(self):
        ticket = self.ticket({"frame_number": "EMXP2026009999"}, {"unlisted_bike": lambda: dict(BIKE)})
        self.assertEqual(ticket["frame_number_source"], UNLISTED_SOURCE)

    def test_a_safety_ticket_carries_the_unlisted_frame(self):
        chat = Chat(replies=[say("Let's check the charger. Is its light on?")])
        unlisted(chat)
        reply = chat.say("my battery is smoking")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        ticket = chat.registry.tickets.tickets[reply.ticket_id]
        self.assertEqual((ticket["frame_number"], ticket["frame_number_source"]), ("EMXP2026009999", UNLISTED_SOURCE))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." PYTHONIOENCODING=utf-8 python -m unittest tests.test_unlisted_bike_runtime`
Expected: ERROR, `ImportError: cannot import name 'UNLISTED_SOURCE'`.

- [ ] **Step 3: The coverage claim check**

In `src/emotorad_ai/guardrails.py`, after `check_coverage_claim`:

```python
def claims_coverage(reply: str) -> bool:
    """Whether the reply says something is, or is not, covered."""
    return bool(_COVERED_CLAIM.search(reply or "") or _NOT_COVERED_CLAIM.search(reply or ""))
```

- [ ] **Step 4: The ticket tool**

In `src/emotorad_ai/tools/mocks.py`, before `def _owned_bike(`:

```python
# A ticket on a bike the customer gave because it is not in their list (spec
# 2026-10-01, unlisted bike).
UNLISTED_SOURCE = "given by the customer; not registered on this number"


def _unlisted_ticket_bike(
    frame_number: Optional[str], unlisted_bike: Optional[Dict[str, Optional[str]]]
) -> Optional[Dict[str, Any]]:
    """The conversation's unlisted bike, for a ticket that names no frame
    number or names that bike's; None otherwise (a listed bike's frame number
    is resolved as before)."""
    if not unlisted_bike:
        return None
    frame = unlisted_bike.get("frame_number")
    if frame_number and re.sub(r"\s+", "", frame_number).upper() != (frame or ""):
        return None
    return {"frame_number": frame, "product_name": unlisted_bike.get("model"), "frame_number_source": UNLISTED_SOURCE}
```

In `create_support_ticket`: `optional_injects=("evidence_seen", "selected_bike")` becomes `optional_injects=("evidence_seen", "selected_bike", "unlisted_bike")`; the function gains the parameter `unlisted_bike: Optional[Dict[str, Optional[str]]] = None` after `selected_bike`; and

```python
        bike = _owned_bike(phone, frame_number, bikes_on, allow_rider_read=True, selected=selected_bike)
```

becomes

```python
        bike = _unlisted_ticket_bike(frame_number, unlisted_bike) or _owned_bike(
            phone, frame_number, bikes_on, allow_rider_read=True, selected=selected_bike)
```

- [ ] **Step 5: The runtime**

In `src/emotorad_ai/runtime.py`:

- Imports: add `claims_coverage` to the `from .guardrails import (...)` list, and `from .triage import TriageAgent, bike_ref` becomes `from .triage import TriageAgent, bike_ref, unlisted_as_bike, unlisted_context`.
- `_selected_bike`: at the top of the method body add

```python
        if state.unlisted_bike:
            # Not a listed bike, and never the one bike the customer rejected.
            return unlisted_as_bike(state.unlisted_bike)
```

- In `_run`, the `agent.run(` call: `state.history, state.context_block or "",` becomes `state.history, (state.context_block or "") + unlisted_context(state.unlisted_bike),`, and the `facts={` dict gains

```python
                # A bike the customer gave because it is not in their list:
                # the ticket tool puts its frame number on a ticket.
                "unlisted_bike": lambda: state.unlisted_bike,
```

- `_post_checks`: after `coverage = check_coverage_claim(text, results)` add

```python
        if state.unlisted_bike and claims_coverage(text):
            # No record of this bike on the number (spec 2026-10-01, unlisted
            # bike): the listed bikes' cover says nothing about it.
            coverage = CoverageCheck(blocked=True, reason="unlisted_bike", claimed="coverage", actual="no record")
```

- `_raise_safety_ticket`: after `late: Dict[str, Any] = {}` add

```python
        if state.unlisted_bike:
            late["unlisted_bike"] = lambda: state.unlisted_bike
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." PYTHONIOENCODING=utf-8 python -m unittest tests.test_unlisted_bike_runtime tests.test_unlisted_bike tests.test_triage`
Expected: OK.

- [ ] **Step 7: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/runtime.py src/emotorad_ai/guardrails.py src/emotorad_ai/tools/mocks.py tests/test_unlisted_bike_runtime.py
git commit -m "fix: the agents, the warranty guard and tickets use the bike not in the list"
```
