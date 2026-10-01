# Greeting, Going Back, Bike Confirmation and Photo Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot greets a greeting back, lets the customer go back from every step (another number, another bike, the list again, a fresh start), confirms an unlisted bike once before it is final, and never lets a photo of a hazard go unremarked because its safety check timed out.

**Architecture:** A new module, `navigation.py`, holds the fixed phrases and texts. A new graph node, `navigation_gate`, sits between `safety_gate` and `handoff_gate` and handles going back after verification; the verify step handles "change number" before it. Triage gains one phase, `AWAITING_BIKE_CONFIRMATION`, entered after the customer said their bike is not in the list. The API gives each photo check two tries inside a 30-second deadline and marks a message whose photo got no answer; the runtime then tells the agent, and every customer agent's prompt gains a photo safety paragraph.

**Tech Stack:** Python 3.12, unittest, LangGraph (the turn graph), FastAPI (the web chat API).

**Spec:** `docs/superpowers/specs/2026-10-02-greeting-going-back-confirm-design.md`

## Global Constraints

- Greeting text, exactly: `"Hi there! I'm EMotorad's virtual assistant, an AI. How can I help with your bike today?"` (`navigation.GREETING_TEXT`).
- The first number question, exactly: `"Happy to help with that. First I need to confirm it's you: what's the mobile number your bike is registered on?"` (`verify_first.ASK_NUMBER`).
- Another number asked for: `"No problem. What's the right mobile number?"` (`verify_first.CHANGE_NUMBER`).
- A signed-in rider who asks to change the number: `"You're signed in to the app with your number, so I can't change it here."` on the `amiigo_app` and `website_chat` channels; `"This chat is linked to your number, so I can't change it here."` on any other.
- Back to the list: `"No problem. "` then `which_bike_text(bikes)`; a fresh start: `"No problem, let's start again. "` then `which_bike_text(bikes)`.
- Confirming the customer's own details: `"Just to confirm: your bike is the <model>, frame <frame>. Is that right?"`; model only: `"Just to confirm: your bike is the <model>, frame number not given. Is that right?"`; frame only: `"Just to confirm: your bike's frame number is <frame>, model not given. Is that right?"`.
- A listed bike's frame number given: `"That frame number is the <model (colour)> in your list. Is that the bike?"`.
- After a no: `"Which is wrong, the frame number or the model?"`.
- An answer that is neither yes nor no is asked again once (`"Sorry, I didn't catch that. <question> Please reply yes or no."`), then taken as no.
- The unchecked-photo line, exactly: `"A photo in this message could not be safety-checked. If it shows smoke, flames, swelling, leaking or sparks, tell the customer to stop using and charging the bike and hand over."` It goes in the agent's context only, never in an attachment's summary.
- The no-pictures rule's first sentences, exactly: `"You cannot send pictures or videos to the customer in this chat. The customer can send you photos and videos."`
- `PHOTO_CHECK_DEADLINE_SECONDS = 30.0`; each photo gets at most two tries inside it.
- Ruling made while planning (spec section 1, the app greeting): the spec gives a signed-in app rider the plain "Hi there! How can I help with your bike today?". The runtime must put the AI line on every conversation's first reply (`disclosure.py`, EU AI Act Article 50), so that text would arrive as "Hi, I'm EMotorad's virtual assistant — an AI, not a person. Hi there! How can I help…", greeting twice. The app rider therefore gets the same `GREETING_TEXT` as the website, which carries the AI line once. Cost if wrong: one constant.
- British English. No em dashes in new code comments or texts. Files are LF: write with Python `write_bytes`, or normalise after the Write tool with `python -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))" <file>`.
- `ENV` below means: `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;."`, run from the repo root.
- Whole suite: `ENV python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`, which fail only on this Windows machine; copy `suite.py` from the session scratchpad into the plan's workspace at setup).
- Existing tests that open with a bare greeting and then expect the number question, or the bike list, open with `"my battery isn't charging"` (or `"can you help?"` in triage tests) instead. That is the behaviour change, not a regression.

## Review Focus

1. **"wrong number" while a bike is being confirmed** means its frame number, not the mobile number: the customer answering "Which is wrong, the frame number or the model?" must not lose their verification. Test: Task 4 `test_wrong_number_while_confirming_is_the_frame_number`.
2. **The old bike's facts after a change of bike**: a warranty lookup, a photo seen, or the video asks for bike 1 must not let the agent claim cover or order a part for bike 2. Tests: Task 1 `test_forget_bike_drops_what_was_learnt_about_it`, Task 4 `test_a_change_of_bike_forgets_the_old_bikes_cover`.
3. **A greeting that is not only a greeting**: "hi" with a photo, or "hi, my battery isn't charging", must go to the number question (with the photo safety line), never the greeting. Tests: Task 2 `test_a_greeting_with_a_photo_asks_for_the_number`, `test_a_greeting_with_the_problem_asks_for_the_number`.
4. **A new number in the same message as "change number"** ("change number to 98765 43210"): the code goes to the new number on that turn, and the number never reaches the model's history or the transcript. Test: Task 4 `test_a_new_number_in_the_same_message_gets_its_code`.
5. **"restart?" mid-troubleshooting**, the customer echoing "restart the bike", is not a fresh start. Test: Task 1 `test_start_over_phrases` (its must-not list).

---

### Task 1: The navigation vocabulary and the state it needs

**Files:**
- Create: `src/emotorad_ai/navigation.py`
- Modify: `src/emotorad_ai/conversation.py` (phases near line 33, the dataclass fields near line 63, a method after `select_bike` near line 203)
- Test: `tests/test_navigation.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `navigation.GREETING_TEXT: str`, `BACK_TO_LIST: str` (`"No problem."`), `START_AGAIN: str` (`"No problem, let's start again."`), `NUMBER_FIXED_APP: str`, `NUMBER_FIXED: str`, `APP_SIGN_IN_CHANNELS: tuple`.
  - `navigation.is_greeting_only(text: str) -> bool`, `wants_change_number(text: str) -> bool`, `wants_change_bike(text: str) -> bool`, `wants_list(text: str) -> bool`, `wants_start_over(text: str) -> bool`, `names_the_mobile(text: str) -> bool`.
  - `conversation.AWAITING_BIKE_CONFIRMATION = "awaiting_bike_confirmation"`, in `PHASES`.
  - `ConversationState.bike_confirmation: Optional[Dict[str, Any]] = None`, shaped `{"kind": "unlisted" | "listed", "ref": Optional[str], "step": "confirm" | "which_wrong", "unclear": int}`.
  - `ConversationState.forget_bike() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_navigation.py`:

```python
"""The phrases for going back, and the greeting (spec 2026-10-02)."""

import unittest

from emotorad_ai.conversation import AWAITING_BIKE_CONFIRMATION, ConversationState
from emotorad_ai.navigation import (
    GREETING_TEXT,
    is_greeting_only,
    names_the_mobile,
    wants_change_bike,
    wants_change_number,
    wants_list,
    wants_start_over,
)


class GreetingTests(unittest.TestCase):
    def test_greetings(self):
        for text in ("hi", "Hi!", "hii", "hiii 👋", "hello", "Hello?", "hey", "hey there", "namaste",
                     "Namaskar", "good morning", "Good evening!", "hi team", "नमस्ते"):
            self.assertTrue(is_greeting_only(text), text)

    def test_what_is_not_only_a_greeting(self):
        for text in ("hi, my battery isn't charging", "hello my bike won't start", "high", "history",
                     "hindi", "", "  ", "ok", "yes"):
            self.assertFalse(is_greeting_only(text), text)

    def test_the_greeting_says_it_is_an_ai_once(self):
        self.assertEqual(GREETING_TEXT,
                         "Hi there! I'm EMotorad's virtual assistant, an AI. How can I help with your bike today?")


class ChangeNumberTests(unittest.TestCase):
    def test_change_number_phrases(self):
        for text in ("wrong number", "Wrong number!", "change number", "change my number",
                     "I want to change my number", "use another number", "use a different number",
                     "not my number", "that's not my number", "galat number", "number galat hai",
                     "change number to 98765 43210", "ok, wrong number"):
            self.assertTrue(wants_change_number(text), text)

    def test_what_is_not(self):
        for text in ("my number is 9876543210", "the frame number is wrong", "wrong frame number",
                     "what is the number for service?", "the number on the sticker is faded", "number 2"):
            self.assertFalse(wants_change_number(text), text)

    def test_names_the_mobile(self):
        self.assertTrue(names_the_mobile("wrong mobile number"))
        self.assertTrue(names_the_mobile("change my phone number"))
        self.assertFalse(names_the_mobile("wrong number"))


class ChangeBikeTests(unittest.TestCase):
    def test_change_bike_phrases(self):
        for text in ("not this one", "Not this bike", "wrong bike", "change bike", "change the bike",
                     "different bike", "other bike", "another bike", "doosri bike", "ye wali nahi",
                     "no, not this one", "it's my other bike"):
            self.assertTrue(wants_change_bike(text), text)

    def test_what_is_not(self):
        for text in ("my other bike works fine with this charger", "not this time", "this one has a light",
                     "wrong charger", "another one?", "not this", "the other one is fine"):
            self.assertFalse(wants_change_bike(text), text)


class ListTests(unittest.TestCase):
    def test_list_phrases(self):
        for text in ("go back", "back", "Back please", "show the list", "show me the list again",
                     "the options", "list", "wapas"):
            self.assertTrue(wants_list(text), text)

    def test_what_is_not(self):
        for text in ("the back wheel is wobbly", "my bike's back light", "go back home",
                     "the list price", "what options for a charger?"):
            self.assertFalse(wants_list(text), text)


class StartOverTests(unittest.TestCase):
    def test_start_over_phrases(self):
        for text in ("start over", "Start again", "restart", "let's start again", "from the beginning",
                     "shuru se", "can I start over?"):
            self.assertTrue(wants_start_over(text), text)
        for text in ("restart the bike", "the bike won't start again", "how do I restart the display?",
                     "restart?", "Restart?"):
            self.assertFalse(wants_start_over(text), text)


class StateTests(unittest.TestCase):
    def test_the_confirmation_phase_saves_and_loads(self):
        state = ConversationState("c1")
        state.move_to(AWAITING_BIKE_CONFIRMATION, "confirm_unlisted")
        state.bike_confirmation = {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0}
        loaded = ConversationState.from_json(state.to_json())
        self.assertEqual(loaded.phase, AWAITING_BIKE_CONFIRMATION)
        self.assertEqual(loaded.bike_confirmation["kind"], "unlisted")

    def test_forget_bike_drops_what_was_learnt_about_it(self):
        state = ConversationState("c1")
        state.select_bike("EMXP2026001234", "EMX Plus")
        state.route_to("battery_support")
        state.unlisted_bike, state.unlisted_asks = {"frame_number": "EMXP2026009999", "model": None}, 2
        state.bike_confirmation = {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0}
        state.sub_category = "battery-wont-charge"
        state.coverage_result = {"covered": True}
        state.evidence_seen, state.evidence_asks, state.video_declined = True, 2, True
        state.placed_order_ids = ["RO-00001"]
        state.pending_topic = "battery"
        state.forget_bike()
        self.assertEqual(
            (state.selected_frame, state.selected_bike_label, state.unlisted_bike, state.unlisted_asks,
             state.bike_confirmation, state.agent, state.sub_category, state.coverage_result,
             state.evidence_seen, state.evidence_asks, state.video_declined),
            (None, None, None, 0, None, None, None, None, False, 0, False))
        # An order placed is real whichever bike comes next; the topic is the
        # caller's to keep or clear.
        self.assertEqual(state.placed_order_ids, ["RO-00001"])
        self.assertEqual(state.pending_topic, "battery")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to watch them fail**

Run: `ENV python -m unittest tests.test_navigation -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'emotorad_ai.navigation'`.

- [ ] **Step 3: Write `navigation.py`**

Create `src/emotorad_ai/navigation.py`:

```python
"""Going back, and the greeting (spec 2026-10-02-greeting-going-back-confirm-design.md).

The person's rule, 1 October 2026: nothing is one-way. A customer can ask for
another number, another bike, the list again or a fresh start from any step,
and a greeting is greeted back. Each phrase is the whole message, give or take
politeness and punctuation: "back" or "another bike" inside a sentence about
the bike ("the back wheel is wobbly", "my other bike works fine with this
charger") is not a request to go anywhere.
"""

from __future__ import annotations

import re

# A greeting-only first message (verify_first.py, triage.py). The AI line is
# part of it, so the runtime's disclosure is not added a second time.
GREETING_TEXT = "Hi there! I'm EMotorad's virtual assistant, an AI. How can I help with your bike today?"
# Before the bike list again (runtime._back_to_list).
BACK_TO_LIST = "No problem."
START_AGAIN = "No problem, let's start again."
# A signed-in rider's number is the sign-in's: there is no code step to redo.
NUMBER_FIXED_APP = "You're signed in to the app with your number, so I can't change it here."
NUMBER_FIXED = "This chat is linked to your number, so I can't change it here."
# Where a signed-in identity comes from the Amiigo app's sign-in.
APP_SIGN_IN_CHANNELS = ("amiigo_app", "website_chat")

_GREETING = re.compile(
    r"^[\W_]*(?:hi+|hello+|helo+|hey+|hiya|heya|namaste|namaskar|namaskaram|hola|yo"
    r"|good\s+(?:morning|afternoon|evening)|नमस्ते|नमस्कार)"
    r"(?:\s+(?:there|team|everyone|emotorad|bot|sir|madam|ji))?[\W_]*$",
    re.IGNORECASE,
)
# What may come before the request ("ok, ", "sorry ", "I want to ") and after
# it (" please", punctuation).
_LEAD = (
    r"^[\W_]*(?:(?:ok|okay|sorry|actually|wait|please|pls|plz|hi|hey|no|nahi)[\W_]+)*"
    r"(?:(?:i\s+want\s+to|i\s+wanna|i'?d\s+like\s+to|i\s+would\s+like\s+to|i\s+need\s+to|can\s+i|could\s+i"
    r"|can\s+we|let\s+me|let'?s|please|pls|plz)\s+)?"
)
_TAIL = r"(?:[\W_]+(?:please|pls|plz))?[\W_]*$"
# A number typed straight after "change number": the verify step reads it.
_NUMBER_AFTER = r"(?:[\s,:-]*(?:to|is|it'?s)?[\s,:-]*\+?\d[\d\s-]{7,16}\d)?"


def _whole(body: str, after: str = "") -> "re.Pattern[str]":
    return re.compile(_LEAD + "(?:" + body + ")" + after + _TAIL, re.IGNORECASE)


_CHANGE_NUMBER = _whole(
    r"(?:(?:that'?s|it'?s|that\s+is|this\s+is)\s+)?(?:the\s+|a\s+)?wrong\s+(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:change|update|edit|correct|fix)\s+(?:the\s+|my\s+)?(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:use|try|give(?:\s+you)?|enter|send)\s+(?:a\s+|an\s+)?(?:another|different|other|new)\s+"
    r"(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|(?:(?:that'?s|it'?s|this\s+is)\s+)?not\s+my\s+(?:mobile\s+|phone\s+)?(?:number|no)"
    r"|galat\s+(?:number|no|nambar)|(?:number|no)\s+galat(?:\s+(?:hai|h|he))?|number\s+badal(?:na|o|do)(?:\s+hai)?"
    r"|गलत\s+नंबर|नंबर\s+गलत(?:\s+है)?",
    after=_NUMBER_AFTER,
)
_CHANGE_BIKE = _whole(
    r"not\s+(?:this|that)\s+(?:one|bike|cycle)"
    r"|(?:(?:it'?s|that'?s)\s+)?(?:the\s+|a\s+)?wrong\s+(?:bike|cycle|one)"
    r"|(?:change|switch)\s+(?:the\s+|my\s+)?(?:bike|cycle)"
    r"|(?:(?:it'?s|i\s+mean|i\s+meant)\s+)?(?:about\s+)?(?:a\s+|the\s+|my\s+)?(?:different|other|another)\s+(?:bike|cycle)"
    r"|a\s+different\s+one"
    r"|(?:doosri|dusri|doosra|dusra)\s+(?:bike|cycle|wali|wala)|(?:ye|yeh)\s+(?:wali|wala)\s+(?:nahi|nahin|nhi)"
    r"|दूसरी\s+(?:बाइक|साइकिल)|(?:ये|यह)\s+वाली\s+नहीं"
)
_LIST = _whole(
    r"(?:go\s+)?back|go\s+to\s+the\s+list|previous(?:\s+step)?"
    r"|(?:show|see|give|send)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:list|options|bikes)(?:\s+again)?"
    r"|(?:the\s+)?(?:list|options)(?:\s+again)?"
    r"|wapas|vapas|wapis|peeche|पीछे|वापस"
)
_START_OVER = _whole(
    r"start\s+(?:over|again|afresh|from\s+(?:the\s+)?(?:beginning|start|scratch))"
    r"|restart(?:\s+(?:the\s+)?(?:chat|conversation))?"
    r"|(?:begin|go)\s+(?:again|from\s+(?:the\s+)?(?:beginning|start))"
    r"|from\s+the\s+(?:beginning|start)"
    r"|(?:phir\s+se\s+)?shuru\s+se(?:\s+(?:karo|karte\s+hai|karte\s+hain))?|phir\s+se\s+shuru(?:\s+karo)?|शुरू\s+से"
)
# "restart?" alone is the customer asking about a step ("restart the bike"),
# not asking to start over.
_RESTART_QUESTION = re.compile(r"^[\W_]*restart[\W_]*\?[\W_]*$", re.IGNORECASE)
_MOBILE = re.compile(r"\b(?:mobile|phone|cell)\b|मोबाइल|फ़ोन|फोन", re.IGNORECASE)


def is_greeting_only(text: str) -> bool:
    return bool(_GREETING.match(text or ""))


def wants_change_number(text: str) -> bool:
    return bool(_CHANGE_NUMBER.match(text or ""))


def wants_change_bike(text: str) -> bool:
    return bool(_CHANGE_BIKE.match(text or ""))


def wants_list(text: str) -> bool:
    return bool(_LIST.match(text or ""))


def wants_start_over(text: str) -> bool:
    return bool(_START_OVER.match(text or "")) and not _RESTART_QUESTION.match(text or "")


def names_the_mobile(text: str) -> bool:
    """Whether the text says it is the mobile number, not a frame number."""
    return bool(_MOBILE.search(text or ""))
```

- [ ] **Step 4: Add the phase, the field and `forget_bike` to `conversation.py`**

After `AWAITING_UNLISTED_BIKE = "awaiting_unlisted_bike"`, add:

```python
# The unlisted bike, or the listed one whose frame number they gave, waiting
# for the customer's yes (spec 2026-10-02, confirming the bike once).
AWAITING_BIKE_CONFIRMATION = "awaiting_bike_confirmation"
```

Replace the `PHASES` line with:

```python
PHASES = (GREETING, AWAITING_BIKE_SELECTION, AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION, AWAITING_ISSUE,
          ROUTED)
```

After the field `unlisted_asks: int = 0`, add:

```python
    # The question waiting for that yes (triage.TriageAgent._resolve_confirmation):
    # {"kind": "unlisted" or "listed", "ref": the listed bike's reference or None,
    # "step": "confirm" or "which_wrong", "unclear": answers that were neither}.
    bike_confirmation: Optional[Dict[str, Any]] = None
```

After the `select_bike` method, add:

```python
    def forget_bike(self) -> None:
        """Back to before a bike was chosen (navigation, spec 2026-10-02). The
        bike goes, with any unlisted one and its confirmation, the agent, and
        what was learnt about that bike, which does not hold for another: its
        warranty lookup, the evidence seen and the asks for it. Orders placed
        stay: they were placed. The topic is the caller's to keep or clear."""
        if self.selected_frame:
            self.transitions.append("bike_forgotten:%s" % self.selected_frame)
        self.selected_frame = None
        self.selected_bike_label = None
        self.unlisted_bike, self.unlisted_asks, self.bike_confirmation = None, 0, None
        self.agent = None
        self.sub_category = None
        self.coverage_result = None
        self.evidence_seen = False
        self.evidence_asks, self.video_declined = 0, False
```

- [ ] **Step 5: Run the tests to watch them pass**

Run: `ENV python -m unittest tests.test_navigation -v`
Expected: PASS, 13 tests.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/navigation.py src/emotorad_ai/conversation.py tests/test_navigation.py
git commit -m "feat: the phrases for going back, and a confirmation phase on the conversation"
```

---

### Task 2: The greeting, the warmer number question, and "change number" in the verify step

**Files:**
- Modify: `src/emotorad_ai/verify_first.py` (imports; `ASK_NUMBER` at line 111; `handle` at lines 194-227; a new `_change_number` method after `_ask_number_again`; `_verified` near line 307)
- Modify: `src/emotorad_ai/triage.py` (imports; `TriageAgent.handle` near line 460)
- Modify: `tests/test_verify_first.py`, `tests/test_verify_first_step.py`, `tests/test_triage.py` (tests that open with a bare greeting)
- Test: `tests/test_greeting.py` (new), `tests/test_verify_first_step.py`

**Interfaces:**
- Consumes: `navigation.GREETING_TEXT`, `is_greeting_only`, `wants_change_number` (Task 1).
- Produces:
  - `verify_first.ASK_NUMBER` (new text), `verify_first.CHANGE_NUMBER: str`.
  - `VerifyFirst.handle` outcomes `"greeting"` (step not started; `state.verify_step` stays None) and `"change_number"` (`state.verify_step == "number"`, the pending code cancelled).
  - Triage reason `"greeting"` with `reply == GREETING_TEXT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_greeting.py`:

```python
"""A greeting is greeted back (spec 2026-10-02)."""

import unittest

from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.navigation import GREETING_TEXT
from emotorad_ai.verify_first import ASK_NUMBER, PHOTO_SAFETY
from tests.test_verify_first import RIDER, Chat


class WebsiteGreetingTests(unittest.TestCase):
    def test_a_greeting_is_greeted_back_with_the_ai_line_once(self):
        reply = Chat().say("hi")
        self.assertEqual((reply.handled_by, reply.text), ("verify_first:greeting", GREETING_TEXT))
        self.assertNotIn(DISCLOSURE_TEXT, reply.text)

    def test_the_next_message_asks_for_the_number_and_keeps_the_problem(self):
        chat = Chat()
        chat.say("hello!")
        reply = chat.say("my battery isn't charging")
        self.assertEqual((reply.handled_by, reply.text), ("verify_first:ask_number", ASK_NUMBER))
        self.assertEqual(chat.state().pending_topic, "battery")

    def test_a_second_greeting_asks_for_the_number(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say("hi").handled_by, "verify_first:ask_number")

    def test_a_greeting_with_the_problem_asks_for_the_number(self):
        reply = Chat().say("hi, my battery isn't charging")
        self.assertEqual(reply.handled_by, "verify_first:ask_number")
        self.assertTrue(reply.text.endswith(ASK_NUMBER), reply.text)

    def test_a_greeting_with_a_photo_asks_for_the_number(self):
        reply = Chat().say("hi", photo=True)
        self.assertEqual(reply.handled_by, "verify_first:ask_number")
        self.assertIn(PHOTO_SAFETY, reply.text)

    def test_a_number_after_the_greeting_gets_a_code(self):
        chat = Chat()
        chat.say("hi")
        self.assertEqual(chat.say(RIDER[3:]).handled_by, "verify_first:code_sent")


class SignedInGreetingTests(unittest.TestCase):
    def test_a_signed_in_rider_who_greets(self):
        chat = Chat()
        rider = Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1")
        reply = chat.say("good morning", identity=rider)
        self.assertEqual((reply.handled_by, reply.text), ("triage", GREETING_TEXT))
        self.assertIn("I found 2 bikes", chat.say("my battery isn't charging", identity=rider).text)


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_verify_first_step.py`, change the import line to:

```python
from emotorad_ai.navigation import GREETING_TEXT
from emotorad_ai.verify_first import ASK_NUMBER, CHANGE_NUMBER, PHOTO_SAFETY, VerifyFirst
```

and add to `StepTests`:

```python
    def test_a_greeting_is_greeted_back_and_the_step_waits(self):
        reply = self.step.handle(message("hi"), self.state)
        self.assertEqual((reply.outcome, reply.text), ("greeting", GREETING_TEXT))
        self.assertIsNone(self.state.verify_step)

    def test_change_number_at_the_code_step_cancels_the_code(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.step.handle(message("9700000010"), self.state)
        reply = self.step.handle(message("wrong number"), self.state)
        self.assertEqual((reply.outcome, reply.text), ("change_number", CHANGE_NUMBER))
        self.assertIsNone(self.store.pending_code("c1"))
        self.assertEqual(self.state.verify_step, "number")

    def test_change_number_at_the_number_step(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.assertEqual(self.step.handle(message("change number"), self.state).outcome, "change_number")

    def test_resend_is_not_change_number(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.step.handle(message("9700000010"), self.state)
        self.assertEqual(self.step.handle(message("please resend"), self.state).outcome, "code_resent")
```

In the same file, in `test_each_outcome_is_logged_without_the_number_or_the_code`, change `self.step.handle(message("hi"), self.state)` to `self.step.handle(message("my battery isn't charging"), self.state)`.

In `tests/test_verify_first.py`, in each of `test_without_the_order_lookup_it_offers_a_person`, `test_a_number_that_is_not_a_mobile_is_refused` and `test_a_stale_code_while_waiting_for_the_number_asks_for_the_number`, change `chat.say("hi")` to `chat.say("my battery isn't charging")`.

In `tests/test_triage.py` near line 153, change `message("hi")` to `message("can you help?")`.

- [ ] **Step 2: Run the tests to watch them fail**

Run: `ENV python -m unittest tests.test_greeting tests.test_verify_first_step -v`
Expected: FAIL. `ImportError: cannot import name 'CHANGE_NUMBER'` for `test_verify_first_step`; in `test_greeting`, `verify_first:ask_number` where `verify_first:greeting` was expected.

- [ ] **Step 3: Change `verify_first.py`**

Add to the imports:

```python
from .navigation import GREETING_TEXT, is_greeting_only, wants_change_number
```

Replace the `ASK_NUMBER` line with:

```python
# Warmer than "before I look into this" (the person, 2026-10-01): help first,
# then why the number is needed.
ASK_NUMBER = ("Happy to help with that. First I need to confirm it's you: what's the mobile number your bike is "
              "registered on?")
# Another number, at the number or the code step, or after verifying (spec
# 2026-10-02, going back).
CHANGE_NUMBER = "No problem. What's the right mobile number?"
```

In `handle`, replace from `if state.verify_step == CODE:` down to the end of the `if state.verify_step is None:` block with:

```python
        if state.verify_step == CODE:
            if phone:
                return self._send_code(message, state, phone)
            if asks_resend(text):
                return self._resend(message, state, text)
            if wants_change_number(text):
                return self._change_number(message, state, text)
            code = find_code(text)
            if code:
                return self._check_code(message, state, code)
            return self._reply(message, state, ASK_CODE.format(masked=state.verify_masked), "ask_code", text)

        if phone:
            return self._send_code(message, state, phone)
        if state.verify_step is None:
            if is_greeting_only(text) and state.turns <= 1 and not message.attachments:
                # Greeted back; the number waits until they say what is wrong
                # (the person, 2026-10-01). The step has not started.
                return self._reply(message, state, GREETING_TEXT, "greeting", text)
            # First contact: the number, and only the number (phone first).
            state.verify_step = NUMBER
            return self._reply(message, state, ASK_NUMBER, "ask_number", text)
        if wants_change_number(text):
            return self._change_number(message, state, text)
```

(The lines after it, from `order = find_order_code(text) ...`, stay as they are.)

After `_ask_number_again`, add:

```python
    def _change_number(self, message: InboundMessage, state: ConversationState, text: str) -> GateReply:
        """Another number, at the number or the code step: a code waiting for
        the old one is cancelled (spec 2026-10-02). The lock is checked first
        in handle(), so a locked step stays locked."""
        self.store.reset(message.conversation_id)
        state.verify_step = NUMBER
        state.verify_masked = None
        return self._reply(message, state, CHANGE_NUMBER, "change_number", text)
```

In `_verified`, replace `state.unlisted_bike, state.unlisted_asks = None, 0` with:

```python
        state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None
```

- [ ] **Step 4: Greet a signed-in rider in `triage.py`**

Add `GREETING` to the `from .conversation import (...)` list, and add:

```python
from .navigation import GREETING_TEXT, is_greeting_only
```

In `TriageAgent.handle`, after the `if state.phase == AWAITING_BIKE_SELECTION:` block and before `pill = message.pill_clicked`, add:

```python
        if (state.phase == GREETING and state.turns <= 1 and state.selected_frame is None
                and not message.pill_clicked and not message.attachments and is_greeting_only(text)):
            # A signed-in rider who only greets (spec 2026-10-02): greeted back,
            # with the AI line the first reply has to carry (disclosure.py).
            return TriageOutcome(reply=GREETING_TEXT, reason="greeting")
```

- [ ] **Step 5: Run the tests to watch them pass**

Run: `ENV python -m unittest tests.test_greeting tests.test_verify_first_step tests.test_verify_first tests.test_triage -v`
Expected: PASS, every test.

- [ ] **Step 6: Run the whole suite**

Run: `ENV python <workspace>/suite.py`
Expected: OK. A failure in a test that opens with a bare greeting and then expects the number question or the bike list is fixed as Global Constraints says (open with `"my battery isn't charging"`); any other failure goes to superpowers:systematic-debugging.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/verify_first.py src/emotorad_ai/triage.py tests/test_greeting.py tests/test_verify_first_step.py tests/test_verify_first.py tests/test_triage.py
git commit -m "feat: greet a greeting back, a warmer number question, and change number in the verify step"
```

---

### Task 3: Confirm the bike once, after "it's not in the list"

**Files:**
- Modify: `src/emotorad_ai/triage.py` (imports; new constants and `confirm_text` after `UNLISTED_REF`; `handle`; `_resolve_selection`'s listed choice; `_collect_unlisted`; `_unlisted_done`; new methods `_choose_listed`, `_confirm_listed`, `_confirm_unlisted`, `_resolve_confirmation`, `_correction`, `_which_wrong`)
- Modify: `tests/test_unlisted_bike.py`, `tests/test_unlisted_bike_runtime.py` (a "yes" after the details)
- Test: `tests/test_bike_confirmation.py` (new)

**Interfaces:**
- Consumes: `AWAITING_BIKE_CONFIRMATION`, `ConversationState.bike_confirmation` (Task 1).
- Produces:
  - `triage.CONFIRM_LISTED`, `ASK_WHICH_WRONG`, `ASK_WHICH_WRONG_AGAIN`, `ASK_RIGHT_FRAME`, `ASK_RIGHT_MODEL`, `CONFIRM_AGAIN`, `CONFIRM_MAX_UNCLEAR = 1`.
  - `triage.confirm_text(bike: Optional[Dict[str, Optional[str]]]) -> str`.
  - Triage reasons `"confirm_bike:unlisted"`, `"confirm_bike:listed"`, `"confirm_bike:unclear"`, `"confirm_bike:no"`, `"confirm_bike:which_unclear"`, `"unlisted_bike:correct"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bike_confirmation.py`:

```python
"""Confirming the bike once, after the customer said it is not in the list
(spec 2026-10-02)."""

import unittest

from emotorad_ai.conversation import AWAITING_BIKE_CONFIRMATION, AWAITING_UNLISTED_BIKE
from emotorad_ai.triage import (
    ASK_FOR_UNLISTED_BIKE,
    ASK_RIGHT_FRAME,
    ASK_RIGHT_MODEL,
    ASK_WHICH_WRONG,
    ASK_WHICH_WRONG_AGAIN,
    TriageAgent,
    confirm_text,
)
from tests.test_triage import TOPIC_AGENTS, message, resolved
from tests.test_unlisted_bike import TWO, choosing

CONFIRM_TREX = "Just to confirm: your bike is the T-Rex Air, frame EMXP2026009999. Is that right?"


class ConfirmTextTests(unittest.TestCase):
    def test_what_is_missing_is_said(self):
        self.assertEqual(confirm_text({"frame_number": "EMXP2026009999", "model": "T-Rex Air"}), CONFIRM_TREX)
        self.assertEqual(confirm_text({"frame_number": None, "model": "Doodle Pro"}),
                         "Just to confirm: your bike is the Doodle Pro, frame number not given. Is that right?")
        self.assertEqual(confirm_text({"frame_number": "DDL32023045678", "model": None}),
                         "Just to confirm: your bike's frame number is DDL32023045678, model not given. "
                         "Is that right?")


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.triage = TriageAgent(TOPIC_AGENTS)

    def say(self, state, text, bikes=TWO):
        return self.triage.handle(message(text), resolved(bikes), state)

    def collected(self, topic="battery"):
        state = choosing(topic)
        self.say(state, "none of these")
        self.assertEqual(self.say(state, "EMXP2026009999, T-Rex Air").reply, CONFIRM_TREX)
        return state

    def test_complete_details_are_confirmed_before_the_bike_is_chosen(self):
        state = self.collected()
        self.assertEqual(state.phase, AWAITING_BIKE_CONFIRMATION)
        self.assertIsNone(state.selected_frame)

    def test_yes_carries_on_to_the_kept_issue(self):
        state = self.collected()
        outcome = self.say(state, "yes")
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "EMXP2026009999")
        self.assertIsNone(state.bike_confirmation)

    def test_the_issue_given_with_the_yes_is_kept(self):
        state = self.collected(topic=None)
        self.assertEqual(self.say(state, "yes, the battery isn't charging").agent, "battery_support")

    def test_no_then_the_model(self):
        state = self.collected()
        self.assertEqual(self.say(state, "no").reply, ASK_WHICH_WRONG)
        self.assertEqual(self.say(state, "the model").reply, ASK_RIGHT_MODEL)
        self.assertEqual(state.unlisted_bike, {"frame_number": "EMXP2026009999", "model": None})
        self.assertEqual(state.phase, AWAITING_UNLISTED_BIKE)
        self.assertEqual(self.say(state, "Doodle V3").reply,
                         "Just to confirm: your bike is the Doodle V3, frame EMXP2026009999. Is that right?")
        self.assertEqual(self.say(state, "yes").agent, "battery_support")

    def test_no_then_the_frame_number(self):
        state = self.collected()
        self.say(state, "no")
        self.assertEqual(self.say(state, "the frame number").reply, ASK_RIGHT_FRAME)
        self.assertEqual(self.say(state, "EMXP2026001111").reply,
                         "Just to confirm: your bike is the T-Rex Air, frame EMXP2026001111. Is that right?")

    def test_no_then_both(self):
        state = self.collected()
        self.say(state, "no")
        self.assertEqual(self.say(state, "both").reply, ASK_FOR_UNLISTED_BIKE)
        self.assertEqual(state.unlisted_bike, {"frame_number": None, "model": None})

    def test_a_correction_in_the_no_itself(self):
        state = self.collected()
        self.assertEqual(self.say(state, "no, it's a Doodle V3").reply,
                         "Just to confirm: your bike is the Doodle V3, frame EMXP2026009999. Is that right?")

    def test_a_wrong_number_answer_is_the_frame_number(self):
        state = self.collected()
        self.say(state, "no")
        self.assertEqual(self.say(state, "wrong number").reply, ASK_RIGHT_FRAME)

    def test_an_unclear_answer_is_asked_once_then_taken_as_no(self):
        state = self.collected()
        again = self.say(state, "hmm")
        self.assertEqual(again.reply, "Sorry, I didn't catch that. " + CONFIRM_TREX + " Please reply yes or no.")
        self.assertEqual(self.say(state, "maybe").reply, ASK_WHICH_WRONG)

    def test_an_unclear_which_part_is_asked_once_then_taken_as_both(self):
        state = self.collected()
        self.say(state, "no")
        self.assertEqual(self.say(state, "hmm").reply, ASK_WHICH_WRONG_AGAIN)
        self.assertEqual(self.say(state, "hmm").reply, ASK_FOR_UNLISTED_BIKE)

    def test_a_listed_frame_is_confirmed_as_that_bike(self):
        state = choosing()
        self.say(state, "none of these")
        outcome = self.say(state, "oh, it's EMXP2025004990 after all")
        self.assertEqual(outcome.reply, "That frame number is the EMX Plus (Grey) in your list. Is that the bike?")
        self.assertEqual(state.phase, AWAITING_BIKE_CONFIRMATION)
        self.assertEqual(self.say(state, "yes").agent, "battery_support")
        self.assertEqual(state.selected_frame, "EMXP2025004990")
        self.assertIsNone(state.unlisted_bike)

    def test_a_listed_match_refused_asks_for_the_details_again(self):
        state = choosing()
        self.say(state, "none of these")
        self.say(state, "oh, it's EMXP2025004990 after all")
        self.assertEqual(self.say(state, "no").reply, ASK_FOR_UNLISTED_BIKE)
        self.assertEqual(state.phase, AWAITING_UNLISTED_BIKE)
        self.assertIsNone(state.selected_frame)

    def test_a_missing_frame_is_said(self):
        state = choosing()
        self.say(state, "none of these")
        self.say(state, "T-Rex Air")
        self.assertEqual(self.say(state, "I can't find it").reply,
                         "Just to confirm: your bike is the T-Rex Air, frame number not given. Is that right?")
        self.assertEqual(self.say(state, "yes").agent, "battery_support")
        self.assertEqual(state.selected_frame, "unlisted")

    def test_a_missing_model_is_said(self):
        state = choosing()
        self.say(state, "DDL32023045678")
        self.say(state, "hmm")
        self.assertEqual(self.say(state, "dunno").reply,
                         "Just to confirm: your bike's frame number is DDL32023045678, model not given. "
                         "Is that right?")

    def test_nothing_given_is_not_confirmed(self):
        state = choosing()
        self.say(state, "none of these")
        self.say(state, "hmm")
        self.assertEqual(self.say(state, "idk").agent, "battery_support")
        self.assertEqual(state.selected_frame, "unlisted")

    def test_a_list_number_while_collecting_needs_no_confirming(self):
        state = choosing()
        self.say(state, "none of these")
        self.assertEqual(self.say(state, "sorry, it's number 2").agent, "battery_support")
```

In `tests/test_unlisted_bike.py`, make these tests answer the confirmation (each reads the outcome after the yes):

- `CollectingTests.test_frame_and_model_in_one_reply_carry_on_to_the_kept_issue`: replace `outcome = self.say(state, "EMXP2026009999, T-Rex Air")` with `self.say(state, "EMXP2026009999, T-Rex Air")` and then `outcome = self.say(state, "yes")`.
- `test_frame_then_model`: replace `outcome = self.say(state, "trex air")` with `self.say(state, "trex air")` and `outcome = self.say(state, "yes")`.
- `test_model_then_frame`: replace the last line with `self.say(state, "EMXP2026009999")` and `self.assertEqual(self.say(state, "yes").agent, "battery_support")`.
- `test_the_whole_answer_in_the_first_reply`: replace `outcome = self.say(state, "not these, mine is a T-Rex Air EMXP2026009999")` with `self.say(state, "not these, mine is a T-Rex Air EMXP2026009999")` and `outcome = self.say(state, "yes")`.
- `test_it_carries_on_after_two_asks`: replace `outcome = self.say(state, "I can't find it")` with `self.say(state, "I can't find it")` and `outcome = self.say(state, "yes")`.
- `test_with_no_issue_yet_it_confirms_and_asks_for_it`: replace `outcome = self.say(state, "EMXP2026009999 T-Rex Air")` with `self.say(state, "EMXP2026009999 T-Rex Air")` and `outcome = self.say(state, "yes")`.
- `test_a_listed_frame_while_collecting_chooses_that_bike`: replace `outcome = self.say(state, "oh, it's EMXP2025004990 after all")` with `self.say(state, "oh, it's EMXP2025004990 after all")` and `outcome = self.say(state, "yes")`.
- `StagingFrameTests.test_frame_and_model_together`: replace `outcome = self.say(state, "TESTEMXP0000069 and model is Doodle Pro")` with `self.say(state, "TESTEMXP0000069 and model is Doodle Pro")` and `outcome = self.say(state, "yes")`.
- `StagingFrameTests.test_a_listed_frame_while_collecting_is_that_bike_and_says_so`: replace `outcome = self.say(state, "TESTEMXP0000001 and model is EMX Plus (Aqua)")` with:

```python
        self.assertEqual(self.say(state, "TESTEMXP0000001 and model is EMX Plus (Aqua)").reply,
                         "That frame number is the EMX Plus (Aqua) in your list. Is that the bike?")
        outcome = self.say(state, "yes")
```

In `tests/test_unlisted_bike_runtime.py`, replace the `unlisted` helper with:

```python
def unlisted(chat, phone=RIDER, not_mine="its not one of these 2"):
    chat.verify(phone, first="hi")
    assert ASK_FOR_UNLISTED_BIKE in chat.say(not_mine).text
    asked = chat.say("EMXP2026009999, T-Rex Air").text
    assert "Just to confirm: your bike is the T-Rex Air, frame EMXP2026009999. Is that right?" in asked, asked
    return chat.say("yes")
```

- [ ] **Step 2: Run the tests to watch them fail**

Run: `ENV python -m unittest tests.test_bike_confirmation tests.test_unlisted_bike tests.test_unlisted_bike_runtime -v`
Expected: ERROR, `ImportError: cannot import name 'ASK_RIGHT_FRAME' from 'emotorad_ai.triage'`; the edited unlisted-bike tests FAIL on the reply to "yes".

- [ ] **Step 3: Add the texts and `confirm_text` to `triage.py`**

Add `AWAITING_BIKE_CONFIRMATION` to the `from .conversation import (...)` list. After `UNLISTED_REF = "unlisted"`, add:

```python
# --- confirming the bike once (spec 2026-10-02) ------------------------------
# After the customer said their bike is not in the list, the bike is confirmed
# once before it is the conversation's: their own details, or the listed bike
# whose frame number they gave.
CONFIRM_LISTED = "That frame number is the {name} in your list. Is that the bike?"
ASK_WHICH_WRONG = "Which is wrong, the frame number or the model?"
ASK_WHICH_WRONG_AGAIN = "Sorry, which part is wrong: the frame number, the model, or both?"
ASK_RIGHT_FRAME = "No problem. What's the right frame number? It's printed on the sticker on the frame."
ASK_RIGHT_MODEL = "No problem. Which model is it?"
CONFIRM_AGAIN = "Sorry, I didn't catch that. {question} Please reply yes or no."
# An answer that is neither yes nor no is asked again this many times, then
# taken as no.
CONFIRM_MAX_UNCLEAR = 1
# A no to "is that right?", in more words than a no to the bike list allows:
# here "no power at all" cannot be the answer.
_CONFIRM_NO = re.compile(
    r"^\W*(?:no|nope|nah|nahi|nahin|nhi|galat|wrong|incorrect|not\s+(?:right|correct|quite))\b"
    r"|^\W*(?:that'?s|it'?s|this\s+is)\s+(?:wrong|incorrect|not\s+(?:right|correct))\b|^\W*नहीं",
    re.IGNORECASE,
)
_WRONG_BOTH = re.compile(r"\b(?:both|dono|donon|everything)\b|दोनों", re.IGNORECASE)
_WRONG_FRAME = re.compile(r"\b(?:frame|chassis|sticker|number)\b|फ्रेम", re.IGNORECASE)
_WRONG_MODEL = re.compile(r"\b(?:model|name)\b|मॉडल", re.IGNORECASE)


def confirm_text(bike: Optional[Dict[str, Optional[str]]]) -> str:
    """The question for the customer's own details, saying what is missing."""
    bike = bike or {}
    model, frame = bike.get("model"), bike.get("frame_number")
    if model and frame:
        details = "your bike is the %s, frame %s" % (model, frame)
    elif model:
        details = "your bike is the %s, frame number not given" % model
    else:
        details = "your bike's frame number is %s, model not given" % frame
    return "Just to confirm: %s. Is that right?" % details
```

- [ ] **Step 4: Route the confirmation phase in `TriageAgent.handle`**

At the top of `handle`, after `text = message.message_text.strip()`, add:

```python
        if state.phase == AWAITING_BIKE_CONFIRMATION:
            return self._resolve_confirmation(text, resolved, state)
```

- [ ] **Step 5: Confirm instead of finalising in `_collect_unlisted`**

Replace the start of `_collect_unlisted`, from `listed = _listed_frame(text, resolved.bikes)` through the `return outcome` of the listed branch, with:

```python
        listed = _listed_frame(text, resolved.bikes)
        if listed is not None:
            # A listed bike's own frame number: confirmed once before it is
            # chosen (spec 2026-10-02).
            return self._confirm_listed(listed, state)
        if not (state.unlisted_bike or {}).get("frame_number"):
            chosen = _ordinal_choice(text, resolved.bikes)
            if chosen is not None:
                # "sorry, it's number 2": a choice from the list the bot showed,
                # which needs no confirming.
                return self._choose_listed(chosen, state)
```

Replace the last line of `_collect_unlisted`, `return self._unlisted_done(state)`, with:

```python
        if missing == ["frame_number", "model"]:
            # Nothing was given to confirm: on with the issue, as before.
            return self._unlisted_done(state)
        return self._confirm_unlisted(state)
```

In `_unlisted_done`, add as its first line `state.bike_confirmation = None`.

In `_resolve_selection`, replace `state.unlisted_bike, state.unlisted_asks = None, 0` with `state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None`.

- [ ] **Step 6: Add the confirmation methods to `TriageAgent`**

After `_collect_unlisted`, add:

```python
    def _choose_listed(self, listed: Dict[str, Any], state: ConversationState) -> TriageOutcome:
        """A listed bike after all: chosen, and said, so the customer knows
        which bike the rest of the chat is about."""
        state.unlisted_bike, state.unlisted_asks, state.bike_confirmation = None, 0, None
        state.select_bike(bike_ref(listed), bike_name(listed))
        state.move_to(AWAITING_ISSUE, "bike_selected")
        topic, source = self._take_pending(state)
        outcome = self._route_or_ask(topic, state, source)
        if outcome.reason == "issue_unknown":
            frame = " frame %s," % listed["frame_number"] if listed.get("frame_number") else ""
            return TriageOutcome(reply="Thanks: that's the %s in the list,%s. %s" % (
                bike_name(listed), frame.rstrip(","), outcome.reply), reason=outcome.reason)
        return outcome

    def _confirm_listed(self, listed: Dict[str, Any], state: ConversationState) -> TriageOutcome:
        state.bike_confirmation = {"kind": "listed", "ref": bike_ref(listed), "step": "confirm", "unclear": 0}
        state.move_to(AWAITING_BIKE_CONFIRMATION, "confirm_listed")
        return TriageOutcome(reply=CONFIRM_LISTED.format(name=bike_name(listed)), reason="confirm_bike:listed")

    def _confirm_unlisted(self, state: ConversationState) -> TriageOutcome:
        state.bike_confirmation = {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0}
        state.move_to(AWAITING_BIKE_CONFIRMATION, "confirm_unlisted")
        return TriageOutcome(reply=confirm_text(state.unlisted_bike), reason="confirm_bike:unlisted")

    def _resolve_confirmation(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> TriageOutcome:
        """The answer to "is that right?" (spec 2026-10-02): a yes makes the
        bike the conversation's; a correction is taken and confirmed; a no asks
        what is wrong, or, for a listed bike, goes back to asking for the
        details; anything else is asked again once, then taken as a no."""
        topic = classify_issue(text)
        if topic and not state.pending_topic:
            state.pending_topic, state.pending_topic_source = topic, "text"
        asked = dict(state.bike_confirmation or {"kind": "unlisted", "ref": None, "step": "confirm", "unclear": 0})
        if asked.get("step") == "which_wrong":
            return self._which_wrong(text, resolved, state, asked)
        listed = None
        if asked.get("kind") == "listed":
            listed = next((bike for bike in resolved.bikes if bike_ref(bike) == asked.get("ref")), None)
            if listed is None:
                # That bike has left the list since: ask for the details again.
                state.bike_confirmation = None
                return self._start_unlisted("", resolved, state)
        if says_yes(text):
            return self._choose_listed(listed, state) if listed is not None else self._unlisted_done(state)
        corrected = self._correction(text, resolved, state)
        if corrected is not None:
            return corrected
        refused = says_no(text) or bool(_CONFIRM_NO.match(text or ""))
        if not refused and asked.get("unclear", 0) < CONFIRM_MAX_UNCLEAR:
            state.bike_confirmation = dict(asked, unclear=asked.get("unclear", 0) + 1)
            question = (CONFIRM_LISTED.format(name=bike_name(listed)) if listed is not None
                        else confirm_text(state.unlisted_bike))
            return TriageOutcome(reply=CONFIRM_AGAIN.format(question=question), reason="confirm_bike:unclear")
        if listed is not None:
            # Not that bike: its details again, from the start.
            state.bike_confirmation = None
            return self._start_unlisted("", resolved, state)
        state.bike_confirmation = dict(asked, step="which_wrong", unclear=0)
        return TriageOutcome(reply=ASK_WHICH_WRONG, reason="confirm_bike:no")

    def _correction(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState
    ) -> Optional[TriageOutcome]:
        """A frame number or a model in the answer itself ("no, it's a Doodle
        V3"): taken, and the bike confirmed again. A listed bike's frame number
        is that bike, confirmed as such. None when the answer gives neither."""
        listed = _listed_frame(text, resolved.bikes)
        if listed is not None and (state.bike_confirmation or {}).get("ref") != bike_ref(listed):
            return self._confirm_listed(listed, state)
        frame, model = _find_frame(text), known_model(text)
        if not (frame or model) or listed is not None:
            return None
        bike = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        if frame:
            bike["frame_number"] = frame
        if model:
            bike["model"] = model
        state.unlisted_bike = bike
        return self._confirm_unlisted(state)

    def _which_wrong(
        self, text: str, resolved: ResolvedIdentity, state: ConversationState, asked: Dict[str, Any]
    ) -> TriageOutcome:
        """The answer to "which is wrong?": that part is asked for again."""
        corrected = self._correction(text, resolved, state)
        if corrected is not None:
            return corrected
        both = bool(_WRONG_BOTH.search(text or ""))
        frame_wrong = both or bool(_WRONG_FRAME.search(text or ""))
        model_wrong = both or bool(_WRONG_MODEL.search(text or ""))
        if not (frame_wrong or model_wrong):
            if asked.get("unclear", 0) < CONFIRM_MAX_UNCLEAR:
                state.bike_confirmation = dict(asked, unclear=asked.get("unclear", 0) + 1)
                return TriageOutcome(reply=ASK_WHICH_WRONG_AGAIN, reason="confirm_bike:which_unclear")
            frame_wrong = model_wrong = True
        bike = dict(state.unlisted_bike or {"frame_number": None, "model": None})
        if frame_wrong:
            bike["frame_number"] = None
        if model_wrong:
            bike["model"] = None
        state.unlisted_bike, state.bike_confirmation = bike, None
        # This ask counts: one more is allowed before carrying on with what it has.
        state.unlisted_asks = 1
        state.move_to(AWAITING_UNLISTED_BIKE, "correcting")
        if frame_wrong and model_wrong:
            reply = ASK_FOR_UNLISTED_BIKE
        elif frame_wrong:
            reply = ASK_RIGHT_FRAME
        else:
            reply = ASK_RIGHT_MODEL
        return TriageOutcome(reply=reply, reason="unlisted_bike:correct")
```

- [ ] **Step 7: Run the tests to watch them pass**

Run: `ENV python -m unittest tests.test_bike_confirmation tests.test_unlisted_bike tests.test_unlisted_bike_runtime tests.test_triage -v`
Expected: PASS, every test.

- [ ] **Step 8: Run the whole suite**

Run: `ENV python <workspace>/suite.py`
Expected: OK.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/triage.py tests/test_bike_confirmation.py tests/test_unlisted_bike.py tests/test_unlisted_bike_runtime.py
git commit -m "feat: confirm the bike once after the customer says it is not in the list"
```

---

### Task 4: The navigation gate in the turn graph

**Files:**
- Modify: `src/emotorad_ai/graph.py` (`TurnNodes`, `NODE_NAMES`, the edges)
- Modify: `src/emotorad_ai/runtime.py` (imports near lines 43, 122 and 123; `_TOPIC_OF_AGENT` after `TOPIC_AGENTS` near line 145; `VERIFY_FIRST_FIELDS` near line 173; the `TurnNodes(...)` registration near line 317; `_node_prepare`'s `choosing` near line 579; new `_node_navigation`, `_navigate_number`, `_back_to_list` after `_node_safety`)
- Modify: `tests/test_graph.py`
- Test: `tests/test_navigation_chat.py` (new)

**Interfaces:**
- Consumes: everything `navigation.py` produces, `ConversationState.forget_bike`, `AWAITING_BIKE_CONFIRMATION` (Task 1); `verify_first.CHANGE_NUMBER` and the verify step's `"change_number"` outcome (Task 2); triage's confirmation phase (Task 3).
- Produces:
  - Graph node `"navigation_gate"` between `"safety_gate"` and `"handoff_gate"`.
  - Reply labels `"navigation:list"`, `"navigation:change_bike"`, `"navigation:start_over"`, `"navigation:number_fixed"`; a change of number after verifying is labelled by the verify step (`"verify_first:change_number"`, or `"verify_first:code_sent"` when the message carries the new number).
  - Event `"navigation"` with `to` (`"number"`, `"number_fixed"`, `"bike_list"`) and, for the list, `why`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_navigation_chat.py`:

```python
"""Going back from every step, through the whole turn (spec 2026-10-02)."""

import unittest

from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION
from emotorad_ai.llm import say
from emotorad_ai.navigation import NUMBER_FIXED_APP
from emotorad_ai.triage import ASK_FOR_UNLISTED_BIKE, ASK_RIGHT_FRAME, ASK_WHICH_WRONG
from emotorad_ai.verify_first import CHANGE_NUMBER
from tests.test_verify_first import ONE_BIKE, RIDER, Chat

SECOND = "DDL32023045678"  # RIDER's second bike
CHECK = say("Is the charger light on?")


def troubleshooting(chat):
    """Verified on RIDER, about the battery, bike 1 chosen, the agent asking."""
    chat.verify()
    assert chat.say("1").handled_by == "battery_support"


class ChangeNumberTests(unittest.TestCase):
    def test_at_the_code_step_the_code_is_cancelled(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        chat.say(RIDER[3:])
        reply = chat.say("wrong number")
        self.assertEqual((reply.handled_by, reply.text), ("verify_first:change_number", CHANGE_NUMBER))
        self.assertIsNone(chat.code())
        self.assertEqual(chat.state().verify_step, "number")
        self.assertEqual(chat.say(ONE_BIKE[3:]).handled_by, "verify_first:code_sent")

    def test_after_too_many_wrong_codes_it_stays_handed_over(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        chat.say(RIDER[3:])
        wrong = "000000" if chat.code() != "000000" else "111111"
        for _ in range(5):
            chat.say(wrong)
        self.assertEqual(chat.say("change number").handled_by, "verify_first:locked")

    def test_at_the_bike_list_the_verification_is_forgotten(self):
        chat = Chat()
        chat.verify()
        reply = chat.say("change number")
        self.assertEqual((reply.handled_by, reply.text), ("verify_first:change_number", CHANGE_NUMBER))
        self.assertIsNone(chat.store.verified_phone("c1"))
        state = chat.state()
        self.assertEqual((state.verify_step, state.selected_frame, state.pending_topic, state.context_block),
                         ("number", None, None, None))
        chat.say(ONE_BIKE[3:])
        verified = chat.say(chat.code())
        self.assertEqual(verified.handled_by, "verify_first:verified")
        self.assertIn("I found 1 bike", verified.text)
        self.assertEqual(chat.state().user_key, "PHONE#" + ONE_BIKE)

    def test_mid_troubleshooting(self):
        chat = Chat(replies=[CHECK])
        troubleshooting(chat)
        self.assertEqual(chat.say("wrong number").handled_by, "verify_first:change_number")
        self.assertIsNone(chat.state().agent)

    def test_a_new_number_in_the_same_message_gets_its_code(self):
        chat = Chat()
        chat.verify()
        reply = chat.say("change number to " + ONE_BIKE[3:])
        self.assertEqual(reply.handled_by, "verify_first:code_sent")
        said = " ".join(turn.text for turn in chat.conversations.transcript("c1"))
        self.assertNotIn(ONE_BIKE[3:], said)
        self.assertNotIn(ONE_BIKE[3:], repr(chat.state().history))

    def test_a_signed_in_rider_is_told_it_cannot_change_here(self):
        chat = Chat()
        reply = chat.say("change number", identity=Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1"))
        self.assertEqual(reply.handled_by, "navigation:number_fixed")
        self.assertTrue(reply.text.endswith(NUMBER_FIXED_APP), reply.text)


class BackToTheListTests(unittest.TestCase):
    def test_go_back_while_giving_an_unlisted_bike(self):
        chat = Chat(replies=[CHECK])
        chat.verify()
        self.assertIn(ASK_FOR_UNLISTED_BIKE, chat.say("none of these").text)
        reply = chat.say("go back")
        self.assertEqual(reply.handled_by, "navigation:list")
        self.assertTrue(reply.text.startswith("No problem. I found 2 bikes on this number:"), reply.text)
        state = chat.state()
        self.assertEqual((state.phase, state.unlisted_bike, state.pending_topic),
                         (AWAITING_BIKE_SELECTION, None, "battery"))
        self.assertEqual(chat.say("1").handled_by, "battery_support")

    def test_show_the_list_while_confirming(self):
        chat = Chat()
        chat.verify()
        chat.say("none of these")
        self.assertIn("Just to confirm", chat.say("EMXP2026009999, T-Rex Air").text)
        self.assertEqual(chat.say("show the list").handled_by, "navigation:list")

    def test_no_while_confirming_asks_which_part(self):
        chat = Chat()
        chat.verify()
        chat.say("none of these")
        chat.say("EMXP2026009999, T-Rex Air")
        self.assertEqual(chat.say("no").text, ASK_WHICH_WRONG)

    def test_wrong_number_while_confirming_is_the_frame_number(self):
        chat = Chat()
        chat.verify()
        chat.say("none of these")
        chat.say("EMXP2026009999, T-Rex Air")
        chat.say("no")
        reply = chat.say("wrong number")
        self.assertEqual((reply.handled_by, reply.text), ("triage", ASK_RIGHT_FRAME))
        self.assertEqual(chat.store.verified_phone("c1"), RIDER)

    def test_not_this_one_mid_troubleshooting_keeps_the_problem(self):
        chat = Chat(replies=[CHECK, say("Let's look at this bike's charger.")])
        troubleshooting(chat)
        reply = chat.say("not this one")
        self.assertEqual(reply.handled_by, "navigation:change_bike")
        self.assertIn("I found 2 bikes", reply.text)
        state = chat.state()
        self.assertEqual((state.agent, state.selected_frame, state.pending_topic), (None, None, "battery"))
        self.assertEqual(chat.say("2").handled_by, "battery_support")
        self.assertEqual(chat.state().selected_frame, SECOND)

    def test_each_change_bike_phrase(self):
        for text in ("not this bike", "wrong bike", "change bike", "change the bike", "different bike",
                     "other bike", "another bike", "doosri bike", "ye wali nahi"):
            chat = Chat(replies=[CHECK])
            troubleshooting(chat)
            self.assertEqual(chat.say(text).handled_by, "navigation:change_bike", text)

    def test_a_change_of_bike_forgets_the_old_bikes_cover(self):
        chat = Chat(replies=[CHECK])
        troubleshooting(chat)
        state = chat.state()
        state.coverage_result, state.evidence_seen = {"covered": True}, True
        chat.conversations.save(state)
        chat.say("wrong bike")
        self.assertEqual((chat.state().coverage_result, chat.state().evidence_seen), (None, False))

    def test_a_sentence_about_another_bike_is_for_the_agent(self):
        chat = Chat(replies=[CHECK, say("Good to know.")])
        troubleshooting(chat)
        self.assertEqual(chat.say("my other bike works fine with this charger").handled_by, "battery_support")

    def test_a_signed_in_rider_can_change_bike(self):
        chat = Chat(replies=[CHECK])
        rider = Identity(strength=VERIFIED, phone=RIDER, em_aid="aid-1")
        chat.say("my battery isn't charging", identity=rider)
        chat.say("1", identity=rider)
        self.assertEqual(chat.say("not this one", identity=rider).handled_by, "navigation:change_bike")


class StartOverTests(unittest.TestCase):
    def test_start_over_clears_the_problem_and_stays_verified(self):
        chat = Chat(replies=[CHECK])
        troubleshooting(chat)
        reply = chat.say("start over")
        self.assertEqual(reply.handled_by, "navigation:start_over")
        self.assertTrue(reply.text.startswith("No problem, let's start again. I found 2 bikes"), reply.text)
        self.assertEqual(chat.store.verified_phone("c1"), RIDER)
        self.assertIsNone(chat.state().pending_topic)
        self.assertIn("What is happening with the bike?", chat.say("1").text)

    def test_nothing_while_a_deletion_waits_for_delete(self):
        chat = Chat()
        chat.verify()
        self.assertEqual(chat.say("delete my data").handled_by, "erasure:confirm")
        self.assertEqual(chat.say("start over").handled_by, "erasure:kept")

    def test_before_verifying_it_is_the_verify_steps(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        self.assertTrue(chat.say("start over").handled_by.startswith("verify_first:"))


if __name__ == "__main__":
    unittest.main()
```

In `tests/test_graph.py`, add to `GraphShapeTests`:

```python
    def test_going_back_comes_after_safety_and_before_the_handoff(self):
        self.assertEqual(NODE_NAMES[1:4], ("safety_gate", "navigation_gate", "handoff_gate"))
```

- [ ] **Step 2: Run the tests to watch them fail**

Run: `ENV python -m unittest tests.test_navigation_chat tests.test_graph -v`
Expected: FAIL. `test_going_back_comes_after_safety_and_before_the_handoff` fails on the tuple; the chat tests fail with `triage` or `battery_support` where `navigation:*` or `verify_first:change_number` was expected.

- [ ] **Step 3: Add the node to `graph.py`**

In `TurnNodes`, after `safety_gate: Node`, add `navigation_gate: Node`. Replace `NODE_NAMES` with:

```python
NODE_NAMES = (
    "prepare", "safety_gate", "navigation_gate", "handoff_gate", "erasure_gate", "verify_gate", "persona_route",
    "jev_classify", "standard_reply", "narrow_agent", "full_agent",
)
```

Replace the `safety_gate` edge with these two:

```python
    graph.add_conditional_edges("safety_gate", _replied_or("navigation_gate"), ["navigation_gate", END])
    # Going back (navigation.py): another number, another bike, the list
    # again, a fresh start. After safety, which always comes first.
    graph.add_conditional_edges("navigation_gate", _replied_or("handoff_gate"), ["handoff_gate", END])
```

In the module docstring, change "then the safety and handoff gates" to "then the safety gate, going back, and the handoff gate".

- [ ] **Step 4: Wire the node in `runtime.py`**

Add `AWAITING_BIKE_CONFIRMATION`, `AWAITING_ISSUE`, `GREETING` and `ROUTED` to the `from .conversation import (...)` list. Replace the triage and verify-first import lines with:

```python
from .navigation import (
    APP_SIGN_IN_CHANNELS,
    BACK_TO_LIST,
    NUMBER_FIXED,
    NUMBER_FIXED_APP,
    START_AGAIN,
    names_the_mobile,
    wants_change_bike,
    wants_change_number,
    wants_list,
    wants_start_over,
)
from .triage import TriageAgent, bike_ref, unlisted_as_bike, unlisted_context, which_bike_text
from .verify_first import CONFIRMED, NUMBER, VerifyFirst
```

(If a name in the conversation list is already imported, leave it once.)

After the `TOPIC_AGENTS = {...}` definition, add:

```python
# The topic an agent works on, kept when the customer changes bike mid-chat.
_TOPIC_OF_AGENT = {agent: topic for topic, agent in TOPIC_AGENTS.items()}
```

In `VERIFY_FIRST_FIELDS`, replace `"unlisted_bike", "unlisted_asks",` with `"unlisted_bike", "unlisted_asks", "bike_confirmation",`.

In the `TurnNodes(...)` registration, after `safety_gate=self._node_safety,`, add `navigation_gate=self._node_navigation,`.

In `_node_prepare`, replace the `choosing = ...` line with:

```python
        choosing = self.verify_gate is not None and state.phase in (
            AWAITING_BIKE_SELECTION, AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION)
```

After `_node_safety`, add:

```python
    def _node_navigation(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        # 1b. Going back (navigation.py, spec 2026-10-02): another number,
        #     another bike, the list again or a fresh start, from any step.
        #     Fixed replies, no model. Never while a deletion waits for DELETE:
        #     that answer has to be the very next message.
        message, state, resolved = turn["message"], turn["conversation"], turn["resolved"]
        if resolved.persona != "customer" or state.erasure_step == erasure_rules.CONFIRMING:
            return {}
        text = message.message_text or ""
        if wants_change_number(text) and (state.phase != AWAITING_BIKE_CONFIRMATION or names_the_mobile(text)):
            # While a bike is confirmed, "wrong number" is its frame number.
            return self._navigate_number(message, state, resolved)
        bikes = resolved.bikes
        if not bikes or not resolved.may_disclose:
            # Not verified yet (the verify step answers), or no list to go back to.
            return {}
        if wants_start_over(text):
            return self._back_to_list(message, state, bikes, keep_topic=False, why="start_over")
        if state.phase in (AWAITING_UNLISTED_BIKE, AWAITING_BIKE_CONFIRMATION) and wants_list(text):
            return self._back_to_list(message, state, bikes, keep_topic=True, why="list")
        chosen = state.selected_frame is not None and state.phase in (AWAITING_ISSUE, ROUTED)
        if chosen and wants_change_bike(text):
            return self._back_to_list(message, state, bikes, keep_topic=True, why="change_bike")
        return {}

    def _navigate_number(
        self, message: InboundMessage, state: ConversationState, resolved: ResolvedIdentity
    ) -> Dict[str, Any]:
        cid = message.conversation_id
        proven = self.verify_gate is not None and self.phone_resolver is not None and bool(self.phone_resolver(cid))
        if proven:
            # Forget who they proved to be; the verify step starts again.
            self.verify_gate.store.reset(cid)
            state.forget_bike()
            state.pending_topic = state.pending_topic_source = None
            state.context_block = None
            state.verify_step, state.verify_masked = NUMBER, None
            state.move_to(GREETING, "change_number")
            self.log.emit("navigation", cid, to="number")
            # The verify step answers: a number in this message gets its code now.
            gate = self.verify_gate.handle(message, state)
            if gate.escalated:
                self.log.escalation(cid, "verification_locked", None)
            shown = replace(message, message_text=gate.model_text)
            return {"reply": self._finish(
                shown, state, gate.text, "verify_first:" + gate.outcome, escalated=gate.escalated,
                metadata={"transcript_text": gate.model_text},
            )}
        if resolved.may_disclose:
            # Signed in: the number is the sign-in's, with no code step to redo.
            self.log.emit("navigation", cid, to="number_fixed")
            fixed = NUMBER_FIXED_APP if message.channel in APP_SIGN_IN_CHANNELS else NUMBER_FIXED
            return {"reply": self._finish(message, state, fixed, "navigation:number_fixed")}
        # Not verified: the verify step answers (verify_first.VerifyFirst.handle).
        return {}

    def _back_to_list(
        self, message: InboundMessage, state: ConversationState, bikes: Sequence[Dict[str, Any]],
        keep_topic: bool, why: str,
    ) -> Dict[str, Any]:
        """Back to the bike list (spec 2026-10-02). The bike and what was
        learnt about it go; the problem stays unless they start over, so
        troubleshooting starts again for the bike they choose."""
        topic = (state.pending_topic or _TOPIC_OF_AGENT.get(state.agent or "")) if keep_topic else None
        source = (state.pending_topic_source or "text") if topic else None
        state.forget_bike()
        state.pending_topic, state.pending_topic_source = topic, source
        state.move_to(AWAITING_BIKE_SELECTION, why)
        self.log.emit("navigation", message.conversation_id, to="bike_list", why=why)
        lead = START_AGAIN if why == "start_over" else BACK_TO_LIST
        return {"reply": self._finish(message, state, lead + " " + which_bike_text(bikes), "navigation:" + why)}
```

(`replace`, `Sequence`, `InboundMessage` and `ResolvedIdentity` are already imported in `runtime.py`; check with `grep -n "^from dataclasses\|^from typing\|InboundMessage\|ResolvedIdentity" src/emotorad_ai/runtime.py | head` and add any that are not.)

- [ ] **Step 5: Run the tests to watch them pass**

Run: `ENV python -m unittest tests.test_navigation_chat tests.test_graph tests.test_verify_first tests.test_erasure_chat -v`
Expected: PASS, every test.

- [ ] **Step 6: Run the whole suite**

Run: `ENV python <workspace>/suite.py`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/graph.py src/emotorad_ai/runtime.py tests/test_navigation_chat.py tests/test_graph.py
git commit -m "feat: go back from any step: another number, another bike, the list, a fresh start"
```

---

### Task 5: Photo safety: two tries, the agent told when there is no answer, and every agent's rule

**Files:**
- Modify: `src/emotorad_ai/photo_check.py` (a constant after `LIVE_HAZARDS`)
- Modify: `src/emotorad_ai/api.py` (`PHOTO_CHECK_DEADLINE_SECONDS` at line 132; `_inbound_attachments` at line 571 and its two returns; `_check_photos` at line 754; `_one_photo` at line 791; `post_message` at lines 813 and 853)
- Modify: `src/emotorad_ai/runtime.py` (`_node_classify` near line 857; `_run` near line 1115)
- Modify: `src/emotorad_ai/agents/base.py` (`NO_PICTURES_RULE` near line 88; a new `PHOTO_SAFETY_RULE`; `Agent.run` near line 176)
- Modify: `tests/test_api_photo_check.py`
- Test: `tests/test_api_photo_check.py`, `tests/test_photo_unchecked.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `photo_check.UNCHECKED_NOTE: str`.
  - `api._check_photos(jobs, conversation_id) -> Tuple[Dict[Tuple[str, Any], str], int]` (notes, photos with no answer); `api._one_photo(load, mime, conversation_id) -> List[str]`; `api._inbound_attachments(body, conversation_id) -> Tuple[List[Dict[str, Any]], int]`.
  - Entry metadata `"photos_unchecked": int` on a message whose photos did not all get an answer.
  - `agents.base.PHOTO_SAFETY_RULE: str`; the new `NO_PICTURES_RULE`.
  - Route reason `"photo_unchecked"` (path `full`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_api_photo_check.py`, add `from emotorad_ai import photo_check` to the imports, and replace `FakeChecker` with:

```python
class FakeChecker:
    """Answers each photo, in order, with a list of live hazards. `fail_first`
    is raised by the first call only, as a timeout on the first try is."""

    provider = "openrouter"

    def __init__(self, answers=(["swelling", "smoke"],), error=None, delay=0.0, fail_first=None):
        self.answers, self.error, self.delay, self.fail_first = list(answers), error, delay, fail_first
        self.seen = []

    def check(self, data, mime):
        self.seen.append((len(data), mime))
        n = len(self.seen)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_first is not None and n == 1:
            raise self.fail_first
        if self.error is not None:
            raise self.error
        return self.answers[min(n, len(self.answers)) - 1]
```

Add to `PhotoCheckTests`:

```python
    def handled_messages(self, checker, attachments):
        seen, real = [], self.api.runtime.handle
        with mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or real(m)):
            reply = self.post(checker, attachments)
        return reply, seen[0]

    def test_a_check_that_fails_once_is_tried_again(self):
        checker = FakeChecker(fail_first=PhotoCheckError("timeout"))
        reply = self.post(checker, [photo()])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertEqual(len(checker.seen), 2)
        (event,) = [e for e in self.api.log.events if e["event"] == "photo_check_retry"]
        self.assertEqual(event["error"], "timeout")

    def test_a_photo_too_large_is_not_tried_again(self):
        checker = FakeChecker(error=PhotoCheckError("too_large"))
        self.post(checker, [photo()])
        self.assertEqual(len(checker.seen), 1)

    def test_a_photo_with_no_answer_is_marked_on_the_message(self):
        reply, message = self.handled_messages(FakeChecker(error=PhotoCheckError("bad_json")), [photo()])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertEqual(message.entry_metadata.get("photos_unchecked"), 1)
        # Never in a summary: the safety gate scans those, and the note's words
        # are hazard words.
        self.assertNotIn("could not be safety-checked", repr(message.attachments))

    def test_a_photo_past_the_deadline_is_marked_too(self):
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.2):
            _, message = self.handled_messages(FakeChecker(delay=2.0), [photo()])
        self.assertEqual(message.entry_metadata.get("photos_unchecked"), 1)

    def test_an_answered_photo_is_not_marked(self):
        _, message = self.handled_messages(FakeChecker(answers=([],)), [photo()])
        self.assertNotIn("photos_unchecked", message.entry_metadata)

    def test_the_deadline_fits_two_tries(self):
        self.assertEqual(self.api.PHOTO_CHECK_DEADLINE_SECONDS, 30.0)
        self.assertGreaterEqual(self.api.PHOTO_CHECK_DEADLINE_SECONDS, 2 * photo_check.TIMEOUT_SECONDS)
```

Create `tests/test_photo_unchecked.py`:

```python
"""A photo the safety check could not answer, and the agents' photo safety
rule (spec 2026-10-02)."""

import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents import late_warranty
from emotorad_ai.agents.base import PHOTO_SAFETY_RULE
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.decisions import Q_LANGUAGE, Q_STANDARD
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, choose
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.photo_check import UNCHECKED_NOTE
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry
from tests.test_jev_runtime import build
from tests.test_verify_first import Chat, jpeg

TODAY = date(2026, 10, 2)
SWITCH = {"id": "afs/battery/photos/battery-onoff-switch.png", "kind": "image",
          "caption": "The battery On/Off switch"}


class UncheckedPhotoTests(unittest.TestCase):
    def test_the_agent_is_told_on_that_turn_only(self):
        chat = Chat(replies=[say("Is the charger light on?"), say("Thanks for the photo.")])
        chat.verify()
        chat.say("1")
        reply = chat.say("this is what I see", photo=True, photos_unchecked=1)
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIn(UNCHECKED_NOTE, chat.llm.requests[-1]["system"])
        self.assertNotIn(UNCHECKED_NOTE, chat.llm.requests[0]["system"])

    def test_jev_sends_it_to_a_model_not_a_standard_reply(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers={Q_STANDARD: choose("std-thanks", 0.97), Q_LANGUAGE: choose("english", 0.95)})],
            fallback=[say("I can see the photo.")],
        )
        message = adapter.to_message({"conversation_id": "conv-1", "session_token": "sess-ananya",
                                      "text": "my battery won't charge, see this",
                                      "attachments": [{"kind": "image", "url": jpeg()}]})
        reply = runtime.handle(replace(message, entry_metadata=dict(message.entry_metadata, photos_unchecked=1)))
        self.assertNotEqual(reply.handled_by, "standard:std-thanks")
        self.assertEqual(jev.calls, [])
        self.assertIn(UNCHECKED_NOTE, fallback.requests[-1]["system"])


class PromptRuleTests(unittest.TestCase):
    def system_for(self, agent, guide_media=None):
        registry = build_registry(today=TODAY, guide_media=guide_media or {}, sent_media={})
        rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude([say("Thanks.")]),
                     log=EventLog(path=None), resolver=IdentityResolver(registry),
                     conversations=InMemoryConversationStore())
        rt.conversations.get("c1").route_to(agent)
        rt.handle(WebsiteChatAdapter(rt.resolver).to_message(
            {"conversation_id": "c1", "session_token": "sess-ananya", "text": "here is my bike"}))
        return rt.llm.requests[0]["system"]

    def test_the_registration_agent_gets_the_safety_rule_and_the_new_pictures_wording(self):
        system = self.system_for(late_warranty.AGENT_NAME)
        self.assertIn(PHOTO_SAFETY_RULE, system)
        self.assertIn("You cannot send pictures or videos to the customer in this chat. "
                      "The customer can send you photos and videos.", system)
        self.assertNotIn("no pictures or videos can be sent in this chat", system)

    def test_an_agent_with_guide_pictures_gets_it_too(self):
        self.assertIn(PHOTO_SAFETY_RULE, self.system_for("battery_support", {"battery_onoff_switch": SWITCH}))

    def test_the_rule_names_each_live_hazard(self):
        for word in ("smoke", "flames", "swelling", "leaking fluid", "sparks", "stop using and charging"):
            self.assertIn(word, PHOTO_SAFETY_RULE)
```

- [ ] **Step 2: Run the tests to watch them fail**

Run: `ENV python -m unittest tests.test_api_photo_check tests.test_photo_unchecked -v`
Expected: ERROR, `ImportError: cannot import name 'PHOTO_SAFETY_RULE'` and `cannot import name 'UNCHECKED_NOTE'`; in `test_api_photo_check`, `test_a_check_that_fails_once_is_tried_again` FAILS (`handled_by` is not the safety reply) and `test_the_deadline_fits_two_tries` FAILS (15.0).

- [ ] **Step 3: Add the note to `photo_check.py`**

After `_WORDS = {...}`, add:

```python
# For the agent's context when a photo got no answer in time (spec
# 2026-10-02). Never an attachment's summary: the safety gate scans those, and
# these words would trip it on every unchecked photo.
UNCHECKED_NOTE = ("A photo in this message could not be safety-checked. If it shows smoke, flames, swelling, "
                  "leaking or sparks, tell the customer to stop using and charging the bike and hand over.")
```

- [ ] **Step 4: Two tries, and the count of unanswered photos, in `api.py`**

Replace `PHOTO_CHECK_DEADLINE_SECONDS = 15.0` (and its comment, if it has one) with:

```python
# Two tries of photo_check.TIMEOUT_SECONDS fit inside it. One 15-second try
# under a 15-second deadline timed out on staging and a smoking bike went
# unseen (2026-10-01).
PHOTO_CHECK_DEADLINE_SECONDS = 30.0
```

Change `_inbound_attachments`'s signature to return `Tuple[List[Dict[str, Any]], int]`, add to its docstring "Also returns how many photos the safety check could not answer.", change its early `return []` to `return [], 0`, change `notes = _check_photos(photo_jobs, conversation_id)` to `notes, unchecked = _check_photos(photo_jobs, conversation_id)`, and change its final `return attachments` to `return attachments, unchecked`.

Replace `_check_photos` and `_one_photo` with:

```python
def _check_photos(
    jobs: Sequence[Tuple[Tuple[str, Any], Callable[[], bytes], str]], conversation_id: str
) -> Tuple[Dict[Tuple[str, Any], str], int]:
    """({job key: description} for the photos showing a live hazard, how many
    photos got no answer).

    All checked at the same time, under one deadline, each with up to two
    tries. Never stops the turn: a photo with no answer is logged and counted,
    and the runtime tells the agent (spec 2026-10-02)."""
    if PHOTO_CHECKER is None or not jobs:
        return {}, 0
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="photo-check")
    futures = {pool.submit(_one_photo, load, mime, conversation_id): key for key, load, mime in jobs}
    done, pending = wait(futures, timeout=PHOTO_CHECK_DEADLINE_SECONDS)
    # A check still running is abandoned, not waited for.
    pool.shutdown(wait=False, cancel_futures=True)
    unchecked = len(pending)
    for _ in pending:
        log.emit("photo_check_skipped", conversation_id, error="timeout")
    notes: Dict[Tuple[str, Any], str] = {}
    for future in done:
        try:
            hazards = future.result()
        except photo_check.PhotoCheckError as exc:
            log.emit("photo_check_skipped", conversation_id, error=str(exc))
            unchecked += 1
            continue
        except Exception as exc:
            log.emit("photo_check_skipped", conversation_id, error=type(exc).__name__)
            unchecked += 1
            continue
        # Customer content, as a video's description is: only on a staging
        # box with dev codes on.
        if DEV_CODES:
            log.emit("photo_check", conversation_id, hazards=list(hazards))
        note = photo_check.describe(hazards)
        if note:
            notes[futures[future]] = note
    return notes, unchecked


def _one_photo(load: Callable[[], bytes], mime: str, conversation_id: str) -> List[str]:
    """The photo's live hazards, with a second try when the first fails (the
    first try timed out on staging, 2026-10-01). A photo too large to send is
    not tried again: it would fail the same way."""
    data = load()
    try:
        return PHOTO_CHECKER.check(data, mime)
    except photo_check.PhotoCheckError as exc:
        if str(exc) == "too_large":
            raise
        log.emit("photo_check_retry", conversation_id, error=str(exc))
    except Exception as exc:
        log.emit("photo_check_retry", conversation_id, error=type(exc).__name__)
    return PHOTO_CHECKER.check(data, mime)
```

In `post_message`, change `attachments = _inbound_attachments(body, conversation_id)` to `attachments, unchecked = _inbound_attachments(body, conversation_id)`, and after the `if place is not None: extra["origin"] = ...` block add:

```python
    if unchecked:
        # The runtime tells the agent (Runtime._run); never a summary.
        extra["photos_unchecked"] = unchecked
```

- [ ] **Step 5: Tell the agent, and keep it off the standard path, in `runtime.py`**

Add `from . import photo_check` next to the other `from . import ...` lines.

In `_node_classify`, after the `pinned_agent` block, add:

```python
        if message.entry_metadata.get("photos_unchecked"):
            # A photo the safety check could not answer: a model looks at it,
            # told so (_run), never a standard reply.
            return {"route": Route(path="full", reasons=("photo_unchecked",))}
```

In `_run`, replace the `turn = agent.run(` call's first lines

```python
        turn = agent.run(
            message, agent_view, state.history,
            (without_bikes(state.context_block or "") if state.unlisted_bike else (state.context_block or ""))
            + unlisted_context(state.unlisted_bike),
```

with

```python
        context = ((without_bikes(state.context_block or "") if state.unlisted_bike else (state.context_block or ""))
                   + unlisted_context(state.unlisted_bike))
        if message.entry_metadata.get("photos_unchecked"):
            # This turn only (spec 2026-10-02).
            context += "\n\n" + photo_check.UNCHECKED_NOTE
        turn = agent.run(
            message, agent_view, state.history, context,
```

(the rest of the call, from `# Conversation facts the order tool decides on.`, stays as it is).

- [ ] **Step 6: The rules in `agents/base.py`**

Replace `NO_PICTURES_RULE` with:

```python
NO_PICTURES_RULE = """

Pictures: you cannot send pictures or videos to the customer in this chat. The customer can send you photos \
and videos. Never offer to show the customer one; describe every step in words, using only what the \
documented steps say."""

# Every customer agent (spec 2026-10-02): a hazard in a photo or video is a
# safety case whatever the conversation was doing. The registration agent had
# no safety rule, and a photo of a smoking bike went unremarked (staging,
# 2026-10-01).
PHOTO_SAFETY_RULE = """

Safety in photos and videos: if a photo or video shows smoke, flames, swelling, leaking fluid or sparks, that \
is a safety case, whatever else the conversation was doing. Stop, tell the customer to stop using and charging \
the bike now, and hand over to a person."""
```

In `Agent.run`, replace

```python
        if self.definition.one_step:
            # Here, not in each agent's prompt text: last, and outside the
            # base prompt a promotion replaces.
            system += ONE_STEP_RULE
```

with

```python
        if self.definition.one_step:
            # Here, not in each agent's prompt text: outside the base prompt a
            # promotion replaces, with the one-step rule last.
            system += PHOTO_SAFETY_RULE
            system += ONE_STEP_RULE
```

- [ ] **Step 7: Run the tests to watch them pass**

Run: `ENV python -m unittest tests.test_api_photo_check tests.test_photo_unchecked tests.test_guide_media_offers -v`
Expected: PASS, every test.

- [ ] **Step 8: Run the whole suite**

Run: `ENV python <workspace>/suite.py`
Expected: OK. A prompt snapshot or a test that pins the end of the system prompt may need `PHOTO_SAFETY_RULE` before `ONE_STEP_RULE`; that is this task's change, ledgered as a ruling.

- [ ] **Step 9: Commit**

```bash
git add src/emotorad_ai/photo_check.py src/emotorad_ai/api.py src/emotorad_ai/runtime.py src/emotorad_ai/agents/base.py tests/test_api_photo_check.py tests/test_photo_unchecked.py
git commit -m "fix: try each photo check twice, tell the agent when a photo has no answer, and give every agent the photo safety rule"
```
