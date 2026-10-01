# Video-First Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Whenever the bot needs to see a fault it asks for a short video first and a photo only if a video is not possible, and after three asks with nothing back a person takes over.

**Architecture:** A new module, `evidence_asks.py`, says what a bot reply asked for (a video, a photo only, or nothing), whether a customer said a video is not possible, and which line the runtime adds. `Runtime._run`, the one place every agent's reply passes through, adds that line and counts asks per conversation, handing over at the fourth; the evidence post-check counts as an ask too. The prompts and knowledge records ask for a video first.

**Tech Stack:** Python 3.12, unittest, YAML knowledge records, Markdown prompts.

**Spec:** `docs/superpowers/specs/2026-10-01-video-first-evidence-design.md`

## Global Constraints

- `MAX_EVIDENCE_ASKS = 3`: three asks with nothing back are sent; the fourth becomes the hand-over.
- `VIDEO_FIRST_LINE = "If you can, a short video is even better: it shows me more than a photo."`
- `PHOTO_FALLBACK_LINE = "A photo is fine."`
- For a reply written in Devanagari: `VIDEO_FIRST_LINE_HI = "हो सके तो एक छोटा वीडियो भेजें: उसमें फ़ोटो से ज़्यादा दिखता है।"`, `PHOTO_FALLBACK_LINE_HI = "फ़ोटो भी चलेगी।"`
- `EVIDENCE_BLOCKED_MESSAGE = "Before I can take this further I need to see it. Please send a short video of what you're describing. If you can't take a video, a photo will do."`
- The hand-over is `HANDOVER_TEXT` (`agents/base.py`) plus `_already_done(turn)`, escalation reason `evidence_not_forthcoming`.
- The safety path, triage, verify-first, the erasure gate and the standard responses do not change.
- A line the runtime adds is written into the model's history too (`one_step.replace_turn_text`), so the next turn's model knows what the customer was asked.
- British English, no em dashes, in new code comments, prompt text, records and docs (existing em dashes in old text stay).
- Files are LF. Write them with Python `write_bytes`, or normalise CRLF to LF after the Write tool.
- Test command (whole suite): `env -u OPENROUTER_API_KEY -u EMOTORAD_MONGO_URI -u EMOTORAD_STORE -u EMOTORAD_AI_MODE -u EMOTORAD_AI_MEDIA_BUCKET -u EMOTORAD_AMIGO_PG_DSN -u EMOTORAD_AI_BUILD -u EMOTORAD_GEO_DB PYTHONPATH="src;." python <workspace>/suite.py` (the suite minus `tests.test_video` and `tests.test_start`, Windows-only failures; copy `suite.py` from the session scratchpad into the workspace).

## Review Focus

1. A reply that thanks the customer for a video and asks for a photo of a detail ("Thanks for the video. Could you also send a photo of the sticker?"): no video line, since a video just arrived and a still is what is needed (Task 1 `test_no_line_when_the_reply_already_speaks_of_a_video`, Task 2 `test_a_photo_of_a_detail_after_a_video_gets_no_line`).
2. A reply written in Devanagari: the added line is in Hindi, not English (Task 1 `test_a_devanagari_reply_gets_the_hindi_line`, Task 2 `test_a_reply_in_devanagari_gets_the_line_in_hindi`).
3. A customer who says they can send neither photos nor videos: the asks still count and a person takes over at the fourth (Task 2 `test_declining_both_still_reaches_a_person`).
4. A photo that could not be fetched or read: it is not evidence, so it does not start the count again (Task 2 `test_a_photo_nobody_could_see_does_not_start_it_again`).
5. A reply that shows the bot's own guide picture and asks for the customer's video ("Here's a picture of the switch. Could you send a video of yours?"): one video ask, no line (Task 1 `test_the_bots_own_picture_beside_a_video_ask`).

---

### Task 1: What a reply asks for, and what a customer declines

**Files:**
- Create: `src/emotorad_ai/evidence_asks.py`
- Test: `tests/test_evidence_asks.py` (new)

**Interfaces:**
- Produces: `asks_for_media(reply: Optional[str]) -> Optional[str]` (`"video"`, `"photo"` or `None`); `declines_video(text: Optional[str]) -> bool`; `added_line(reply: Optional[str], asked: Optional[str], video_declined: bool) -> Optional[Tuple[str, str]]` returning `(event, line)` with event `"video_first_added"` or `"photo_fallback_added"`, or `None`; constants `MAX_EVIDENCE_ASKS`, `VIDEO_FIRST_LINE`, `PHOTO_FALLBACK_LINE`, `VIDEO_FIRST_LINE_HI`, `PHOTO_FALLBACK_LINE_HI`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evidence_asks.py`:

```python
"""Video first: what a bot reply asks for, and what a customer declines
(spec 2026-10-01-video-first-evidence-design.md)."""

import unittest

from emotorad_ai import evidence_asks as ev


class AsksForMediaTests(unittest.TestCase):
    def test_a_video_ask(self):
        for reply in ("Could you send a short video of the charger plugged in, showing its light?",
                      "Please record a quick clip of the switch being turned on.",
                      "Send me a video of the display when you press the power button.",
                      "Can you share a video or a photo of the port?",
                      "Thanks for the photo. Could you also send a short video of the plug going in?",
                      "Can you show me the light in a video?",
                      "Charger ki light ka ek video bhejiye.",
                      "चार्जर की लाइट का एक छोटा वीडियो भेजें।"):
            self.assertEqual(ev.asks_for_media(reply), "video", reply)

    def test_a_photo_only_ask(self):
        for reply in ("Could you send a photo of the charger LED?",
                      "Please take a picture of the sticker on the frame.",
                      "Can you share a pic of the display?",
                      "Is the light red? Please send a photo of it.",
                      "Ek photo bhejiye.",
                      "डिस्प्ले की फ़ोटो भेजें।"):
            self.assertEqual(ev.asks_for_media(reply), "photo", reply)

    def test_not_an_ask(self):
        for reply in ("Here's a picture of the on/off switch.",
                      "Let me send you a picture of where the switch is.",
                      "Thanks for the video, I can see the light stays red.",
                      "I can see in your video that the display stays dark.",
                      "Your photo shows a clean terminal.",
                      "Is the charger light on?",
                      "Could you take a look at the charger?",
                      "Main aapko switch ki photo bhej raha hoon.",
                      "Video mein light dikhai de rahi hai.",
                      "आपने जो वीडियो भेजा, उसमें लाइट लाल है।",
                      "",
                      None):
            self.assertIsNone(ev.asks_for_media(reply), reply)

    def test_the_bots_own_picture_beside_a_video_ask(self):
        self.assertEqual(ev.asks_for_media("Here's a picture of the switch. Could you send a video of yours?"), "video")


class DeclinesVideoTests(unittest.TestCase):
    def test_a_decline(self):
        for text in ("I can't take a video", "Sorry, I cannot record a video right now", "unable to send video",
                     "my camera doesn't record", "can I send a photo instead?", "only a photo, sorry",
                     "video is not possible", "video isn't possible here", "no video, sorry",
                     "I don't have a video", "I can't send photos or videos", "video nahi ho payega",
                     "वीडियो नहीं बन रहा"):
            self.assertTrue(ev.declines_video(text), text)

    def test_not_a_decline(self):
        for text in ("I've sent a video", "the video is uploading", "here is the video", "video bhej diya",
                     "I can send a video", "no video yet, it is still uploading", "", None):
            self.assertFalse(ev.declines_video(text), text)


class AddedLineTests(unittest.TestCase):
    def test_a_photo_ask_gets_the_video_line(self):
        self.assertEqual(ev.added_line("Could you send a photo of the LED?", "photo", False),
                         ("video_first_added", ev.VIDEO_FIRST_LINE))

    def test_after_a_decline_a_photo_ask_gets_nothing(self):
        self.assertIsNone(ev.added_line("Could you send a photo of the LED?", "photo", True))

    def test_after_a_decline_a_video_ask_gets_the_photo_line(self):
        self.assertEqual(ev.added_line("Could you send a video of the LED?", "video", True),
                         ("photo_fallback_added", ev.PHOTO_FALLBACK_LINE))

    def test_a_video_ask_before_any_decline_gets_nothing(self):
        self.assertIsNone(ev.added_line("Could you send a video of the LED?", "video", False))

    def test_no_line_when_the_reply_already_speaks_of_a_video(self):
        self.assertIsNone(ev.added_line("Thanks for the video. Could you also send a photo of the sticker?",
                                        "photo", False))

    def test_a_devanagari_reply_gets_the_hindi_line(self):
        self.assertEqual(ev.added_line("डिस्प्ले की फ़ोटो भेजें।", "photo", False),
                         ("video_first_added", ev.VIDEO_FIRST_LINE_HI))
        self.assertEqual(ev.added_line("चार्जर का वीडियो भेजें।", "video", True),
                         ("photo_fallback_added", ev.PHOTO_FALLBACK_LINE_HI))

    def test_nothing_asked_gets_nothing(self):
        self.assertIsNone(ev.added_line("Is the light on?", None, False))

    def test_three_asks(self):
        self.assertEqual(ev.MAX_EVIDENCE_ASKS, 3)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_evidence_asks`
Expected: ERROR, `ModuleNotFoundError: No module named 'emotorad_ai.evidence_asks'`.

- [ ] **Step 3: Write the module**

Create `src/emotorad_ai/evidence_asks.py`:

```python
"""Video first (spec 2026-10-01-video-first-evidence-design.md).

When the bot needs to see a fault it asks for a short video first, and a photo
only when the customer cannot take one. The model decides when to ask and what
to show. This module tells the runtime what a reply asked for and when a
customer said a video is not possible, and holds the lines the runtime adds.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Three asks with nothing back are sent; the fourth becomes a hand-over.
MAX_EVIDENCE_ASKS = 3

VIDEO_FIRST_LINE = "If you can, a short video is even better: it shows me more than a photo."
PHOTO_FALLBACK_LINE = "A photo is fine."
# For a reply the model wrote in Devanagari.
VIDEO_FIRST_LINE_HI = "हो सके तो एक छोटा वीडियो भेजें: उसमें फ़ोटो से ज़्यादा दिखता है।"
PHOTO_FALLBACK_LINE_HI = "फ़ोटो भी चलेगी।"

# Sentences end at . ! ? ; a dash, a new line, or the Devanagari full stop.
_SENTENCE = re.compile(r"[^.!?;\n\u2014\u0964]+[.!?\u0964]?")
_VIDEO = re.compile(r"\b(?:videos?|clips?|recordings?)\b|वीडियो", re.IGNORECASE)
_PHOTO = re.compile(
    r"\b(?:photos?|photographs?|pictures?|pics?|images?|snaps?|snapshots?|screenshots?)\b|फ़ोटो|फोटो|तस्वीर",
    re.IGNORECASE,
)
_VERB = r"(?:send|share|upload|attach|record|take|film|shoot|snap|show)"
# A request addressed to the customer. The imperative forms only: "bheja"
# (sent) and "dikhai" (is visible) are not requests.
_ASK = re.compile(
    r"\b(?:could|can|would|will)\s+you\s+(?:please\s+)?(?:also\s+)?(?:quickly\s+)?" + _VERB + r"\b"
    r"|\bplease\s+(?:also\s+)?" + _VERB + r"\b"
    r"|^\W*(?:also\s+)?" + _VERB + r"\b"
    r"|\bbhej(?:o|iye|ein|en|na|do|dein|de)?\b|\bdikha(?:o|iye|ein|en|na|do|dein|de)?\b"
    r"|भेजें|भेजिए|भेजो|भेज\s+दें|भेज\s+दीजिए|दिखाएं|दिखाइए|दिखाओ",
    re.IGNORECASE,
)
# The bot itself sending, in Hinglish: "main aapko photo bhej raha hoon".
_BOT_SENDS = re.compile(r"\b(?:main|mai|maine|hum)\b", re.IGNORECASE)
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

_DECLINES_VIDEO = re.compile(
    r"\b(?:can'?t|cannot|can\s+not|unable\s+to|not\s+able\s+to|won'?t\s+be\s+able\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:take|send|record|shoot|make|upload|do|film|share)\s+(?:\w+\s+){0,3}?videos?\b"
    r"|\b(?:don'?t|do\s+not)\s+have\s+(?:a\s+)?videos?\b"
    r"|\bno\s+videos?\b(?!\s+yet)"
    r"|\bvideos?\s+(?:is\s+|isn'?t\s+|not\s+)(?:not\s+)?possible\b"
    r"|\bcamera\s+(?:doesn'?t|does\s+not|can'?t|cannot|won'?t)\s+record\b"
    r"|\bonly\s+(?:a\s+)?(?:photos?|pictures?|pics?)\b"
    r"|\b(?:photos?|pictures?|pics?)\s+instead\b"
    r"|\bvideo\s+(?:nahi|nahin|nhi)\b|वीडियो\s+नहीं",
    re.IGNORECASE,
)


def asks_for_media(reply: Optional[str]) -> Optional[str]:
    """"video" when a sentence of the reply asks the customer for a video (or
    a video and a photo), "photo" when the sentences that ask name only a
    photo, None when nothing is asked."""
    asked: Optional[str] = None
    for sentence in _SENTENCE.findall(reply or ""):
        if not _ASK.search(sentence) or _BOT_SENDS.search(sentence):
            continue
        if _VIDEO.search(sentence):
            return "video"
        if _PHOTO.search(sentence):
            asked = "photo"
    return asked


def declines_video(text: Optional[str]) -> bool:
    """Whether the customer says a video is not possible."""
    return bool(_DECLINES_VIDEO.search(text or ""))


def added_line(reply: Optional[str], asked: Optional[str], video_declined: bool) -> Optional[Tuple[str, str]]:
    """The line the runtime adds to a reply that asked for evidence, and the
    event it logs, or None.

    A photo-only ask gets the video line, unless the customer said a video is
    not possible, or the reply already speaks of a video (one just arrived and
    a still of a detail is what is needed). A video ask after the customer said
    a video is not possible gets the photo line."""
    text = reply or ""
    hindi = bool(_DEVANAGARI.search(text))
    if asked == "photo" and not video_declined and not _VIDEO.search(text):
        return "video_first_added", VIDEO_FIRST_LINE_HI if hindi else VIDEO_FIRST_LINE
    if asked == "video" and video_declined:
        return "photo_fallback_added", PHOTO_FALLBACK_LINE_HI if hindi else PHOTO_FALLBACK_LINE
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_evidence_asks`
Expected: OK.

- [ ] **Step 5: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK.

```bash
git add src/emotorad_ai/evidence_asks.py tests/test_evidence_asks.py
git commit -m "feat: evidence_asks says what a reply asked for and when a customer declines video"
```

---

### Task 2: The runtime adds the line and counts the asks

**Files:**
- Modify: `src/emotorad_ai/conversation.py` (`ConversationState`: add `evidence_asks`, `video_declined`; remove `evidence_asked`)
- Modify: `src/emotorad_ai/runtime.py` (imports; `_note_customer_turn`; `_merge_onto_fresh`; the evidence post-check in `_run`; the new check at the end of `_run`)
- Modify: `src/emotorad_ai/guardrails.py` (`EVIDENCE_BLOCKED_MESSAGE`)
- Modify: `tests/test_evidence_before_ticket.py`, `tests/test_evidence_only_what_was_seen.py` (the two tests that pinned a hand-over on the second blocked conclusion)
- Test: `tests/test_video_first.py` (new)

**Interfaces:**
- Consumes: Task 1's `asks_for_media`, `declines_video`, `added_line`, `MAX_EVIDENCE_ASKS`, `VIDEO_FIRST_LINE`, `PHOTO_FALLBACK_LINE`, `VIDEO_FIRST_LINE_HI`.
- Produces: `ConversationState.evidence_asks: int`, `ConversationState.video_declined: bool`; handled_by `"guardrail:evidence_not_forthcoming"` for the fourth ask.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_video_first.py`:

```python
"""Video first in the runtime (spec 2026-10-01-video-first-evidence-design.md)."""

import unittest

from emotorad_ai.agents.base import HANDOVER_TEXT
from emotorad_ai.evidence_asks import PHOTO_FALLBACK_LINE, VIDEO_FIRST_LINE, VIDEO_FIRST_LINE_HI
from emotorad_ai.guardrails import EVIDENCE_BLOCKED_MESSAGE
from emotorad_ai.jev import JevDecision
from emotorad_ai.llm import say
from tests.test_evidence_only_what_was_seen import FAULT, S3_PHOTO, Store, runtime, send
from tests.test_jev_runtime import NARROW, build
from tests.test_jev_runtime import send as jev_send

PHOTO_ASK = "Could you send a photo of the charger light?"
VIDEO_ASK = "Could you send a short video of the charger plugged in, showing its light?"


def state(rt):
    return rt.conversations.peek("c1")


class VideoFirstTests(unittest.TestCase):
    def test_a_photo_only_ask_gets_the_video_line(self):
        rt = runtime([say(PHOTO_ASK)], media_store=Store())
        reply = send(rt, "my battery isn't charging")
        self.assertIn(PHOTO_ASK, reply.text)
        self.assertIn(VIDEO_FIRST_LINE, reply.text)
        self.assertEqual(len([e for e in rt.log.events if e["event"] == "video_first_added"]), 1)
        # The next turn's model sees what the customer was asked.
        self.assertIn(VIDEO_FIRST_LINE, str(state(rt).history[-1]["content"]))

    def test_a_video_ask_is_left_alone(self):
        rt = runtime([say(VIDEO_ASK)], media_store=Store())
        reply = send(rt, "my battery isn't charging")
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)
        self.assertNotIn(PHOTO_FALLBACK_LINE, reply.text)

    def test_after_a_decline_a_video_ask_offers_a_photo(self):
        rt = runtime([say(VIDEO_ASK), say(VIDEO_ASK)], media_store=Store())
        send(rt, "my battery isn't charging")
        reply = send(rt, "sorry, I can't take a video")
        self.assertTrue(state(rt).video_declined)
        self.assertIn(PHOTO_FALLBACK_LINE, reply.text)

    def test_after_a_decline_a_photo_ask_gets_no_video_line(self):
        rt = runtime([say(PHOTO_ASK)], media_store=Store())
        reply = send(rt, "my battery isn't charging, and I can't take a video")
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)

    def test_a_photo_of_a_detail_after_a_video_gets_no_line(self):
        rt = runtime([say("Thanks for the video. Could you also send a photo of the sticker?")], media_store=Store())
        reply = send(rt, "here is the video", [S3_PHOTO])
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)

    def test_a_reply_in_devanagari_gets_the_line_in_hindi(self):
        rt = runtime([say("चार्जर की लाइट की फ़ोटो भेजें।")], media_store=Store())
        reply = send(rt, "चार्ज नहीं हो रहा")
        self.assertIn(VIDEO_FIRST_LINE_HI, reply.text)
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)


class ThreeAsksTests(unittest.TestCase):
    def test_three_asks_then_a_person(self):
        rt = runtime([say(VIDEO_ASK)] * 4, media_store=Store())
        for text in ("my battery isn't charging", "hmm", "ok"):
            self.assertIn(VIDEO_ASK, send(rt, text).text)
        fourth = send(rt, "what now")
        self.assertIn(HANDOVER_TEXT, fourth.text)
        self.assertNotIn(VIDEO_ASK, fourth.text)
        self.assertTrue(fourth.escalated)
        self.assertEqual(fourth.handled_by, "guardrail:evidence_not_forthcoming")

    def test_a_photo_that_arrives_starts_the_count_again(self):
        rt = runtime([say(VIDEO_ASK)] * 4, media_store=Store())
        for text in ("my battery isn't charging", "hmm", "ok"):
            send(rt, text)
        self.assertEqual(state(rt).evidence_asks, 3)
        reply = send(rt, "here it is", [S3_PHOTO])
        self.assertIn(VIDEO_ASK, reply.text)
        self.assertEqual(state(rt).evidence_asks, 1)

    def test_a_photo_nobody_could_see_does_not_start_it_again(self):
        rt = runtime([say(VIDEO_ASK)] * 4, media_store=Store(fail=True))
        for text in ("my battery isn't charging", "hmm", "ok"):
            send(rt, text)
        fourth = send(rt, "here it is", [S3_PHOTO])
        self.assertIn(HANDOVER_TEXT, fourth.text)

    def test_a_blocked_conclusion_counts_as_an_ask(self):
        rt = runtime([say(VIDEO_ASK), say(VIDEO_ASK), say(FAULT), say(FAULT)], media_store=Store())
        send(rt, "my battery isn't charging")
        send(rt, "hmm")
        third = send(rt, "it's still not charging")
        self.assertIn(EVIDENCE_BLOCKED_MESSAGE, third.text)
        self.assertEqual(state(rt).evidence_asks, 3)
        fourth = send(rt, "so is it faulty")
        self.assertIn(HANDOVER_TEXT, fourth.text)
        self.assertTrue(fourth.escalated)

    def test_declining_both_still_reaches_a_person(self):
        rt = runtime([say(PHOTO_ASK)] * 4, media_store=Store())
        send(rt, "my battery isn't charging, and I can't send photos or videos")
        send(rt, "no")
        send(rt, "no")
        self.assertIn(HANDOVER_TEXT, send(rt, "no").text)


class EveryAgentTests(unittest.TestCase):
    def test_the_narrow_agents_photo_ask_gets_the_video_line(self):
        rt, adapter, *_ = build([JevDecision(answers=NARROW)], narrow=[say(PHOTO_ASK)])
        reply = jev_send(rt, adapter, "my battery won't charge")
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertIn(VIDEO_FIRST_LINE, reply.text)

    def test_a_safety_report_gets_no_ask_and_no_count(self):
        rt = runtime([say(PHOTO_ASK)], media_store=Store())
        reply = send(rt, "my battery is smoking")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)
        self.assertEqual(state(rt).evidence_asks, 0)


class BlockedMessageTests(unittest.TestCase):
    def test_it_asks_for_a_video_first(self):
        self.assertEqual(EVIDENCE_BLOCKED_MESSAGE, (
            "Before I can take this further I need to see it. Please send a short video of what you're "
            "describing. If you can't take a video, a photo will do."))
```

In `tests/test_evidence_before_ticket.py`, replace `test_one_photo_request_then_a_person_and_no_ticket_created_without_evidence` with:

```python
    def test_three_asks_then_a_person_and_no_ticket_created_without_evidence(self):
        # Three asks in all (video-first spec, 2026-10-01), then a person.
        rt = runtime([
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"),
            say("I've raised a support ticket for you; the team will be in touch."),
            say("I have raised a support ticket, as you asked."),
            say("I have raised a support ticket, as you asked."),
            say("I have raised a support ticket, as you asked."),
        ], store=routed_store())
        first = send(rt, "room temperature")
        self.assertEqual(rt.registry.tickets.tickets, {})  # refused at the tool
        self.assertIn(EVIDENCE_BLOCKED_MESSAGE, first.text)
        for text in ("please raise a complaint", "raise it please"):
            self.assertIn(EVIDENCE_BLOCKED_MESSAGE, send(rt, text).text)
        fourth = send(rt, "I said raise it")
        self.assertNotIn(EVIDENCE_BLOCKED_MESSAGE, fourth.text)
        self.assertIn(HANDOVER_TEXT, fourth.text)
        self.assertTrue(fourth.escalated)
```

In `tests/test_evidence_only_what_was_seen.py`, replace `test_asked_once_then_a_person_takes_it` with:

```python
    def test_asked_three_times_then_a_person_takes_it(self):
        # Three asks in all (video-first spec, 2026-10-01), then a person.
        rt = runtime([say(FAULT)] * 4, media_store=Store(fail=True))
        replies = [send(rt, text, [S3_PHOTO]) for text in ("here is the terminal", "I sent it again", "and again",
                                                            "once more")]
        for reply in replies[:3]:
            self.assertIn(EVIDENCE_BLOCKED_MESSAGE, reply.text)
        self.assertIn(HANDOVER_TEXT, replies[3].text)
        self.assertTrue(replies[3].escalated)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_video_first tests.test_evidence_before_ticket tests.test_evidence_only_what_was_seen`
Expected: FAIL and ERROR. No `VIDEO_FIRST_LINE` in replies, `AttributeError: 'ConversationState' object has no attribute 'evidence_asks'` / `video_declined`, the old `EVIDENCE_BLOCKED_MESSAGE` text, and the two updated tests getting a hand-over on the second send.

- [ ] **Step 3: The state**

In `src/emotorad_ai/conversation.py`, `ConversationState`, replace

```python
    # The evidence post-check has asked for a photo once already. A second
    # blocked reply hands over to a person rather than asking again.
    evidence_asked: bool = False
```

with

```python
    # Video first (spec 2026-10-01-video-first-evidence-design.md): asks for a
    # video or photo since one last reached the model, the evidence post-check's
    # included, and whether the customer said a video is not possible. Three
    # asks with nothing back are sent; the fourth hands over to a person.
    evidence_asks: int = 0
    video_declined: bool = False
```

- [ ] **Step 4: The blocked message**

In `src/emotorad_ai/guardrails.py`:

```python
EVIDENCE_BLOCKED_MESSAGE = (
    "Before I can take this further I need to see it. Please send a short video of what you're "
    "describing. If you can't take a video, a photo will do."
)
```

- [ ] **Step 5: The runtime**

In `src/emotorad_ai/runtime.py`:

Imports, after the `from .erasure ...` line group (anywhere among the `from .` imports):

```python
from .evidence_asks import MAX_EVIDENCE_ASKS, added_line, asks_for_media, declines_video
```

`_note_customer_turn`: replace

```python
        if shows_media(content):
            state.evidence_seen = True
        elif message.attachments:
```

with

```python
        if declines_video(message.message_text):
            state.video_declined = True
        if shows_media(content):
            state.evidence_seen = True
            # Something to see arrived: the asks start again (video first).
            state.evidence_asks = 0
        elif message.attachments:
```

`_merge_onto_fresh`: after `fresh.evidence_seen = fresh.evidence_seen or ours.evidence_seen` add

```python
        # This turn's count is the newer one, and a decline stands.
        fresh.evidence_asks = ours.evidence_asks
        fresh.video_declined = fresh.video_declined or ours.video_declined
```

The evidence post-check in `_run`: replace `if state.evidence_asked:` with `if state.evidence_asks >= MAX_EVIDENCE_ASKS:`, replace its comment "Asked for a photo once already, and the reply still concludes" with "Asked three times already, and the reply still concludes", and replace `state.evidence_asked = True` with `state.evidence_asks += 1`.

At the end of `_run`, after the deletion-claim check and before `return Reply(`, add:

```python
        # Video first (spec 2026-10-01-video-first-evidence-design.md): the
        # model decides when to ask to see something; every ask is a video
        # first, and three asks with nothing back hand the chat to a person.
        asked = asks_for_media(turn.text)
        if asked is not None:
            if state.evidence_asks >= MAX_EVIDENCE_ASKS:
                self.log.guardrail(message.conversation_id, "evidence_not_forthcoming",
                                   {"suppressed_text": turn.text})
                self.log.escalation(message.conversation_id, "evidence_not_forthcoming", turn.ticket_id)
                return self._finish(
                    message, state, HANDOVER_TEXT + self._already_done(turn), "guardrail:evidence_not_forthcoming",
                    escalated=True, ticket_id=turn.ticket_id,
                    metadata={"suppressed_text": turn.text, "handover": True}, already_in_history=True,
                )
            state.evidence_asks += 1
            added = added_line(turn.text, asked, state.video_declined)
            if added is not None:
                event, line = added
                text = turn.text.rstrip() + "\n\n" + line
                self.log.emit(event, message.conversation_id)
                replace_turn_text(state.history, text)
                turn = replace(turn, text=text)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONPATH="src;." python -m unittest tests.test_video_first tests.test_evidence_before_ticket tests.test_evidence_only_what_was_seen tests.test_evidence_asks`
Expected: OK.

- [ ] **Step 7: Run the whole suite and commit**

Run: the Global Constraints test command. Expected: OK. A test whose scripted reply happens to ask for a photo now sees `VIDEO_FIRST_LINE` added, and one whose conversation asks four times now hands over: read each, update it only if the new behaviour is the right answer for that reply, and record each change as a ruling. `grep -rn "evidence_asked" src tests` prints nothing.

```bash
git add src/emotorad_ai/conversation.py src/emotorad_ai/runtime.py src/emotorad_ai/guardrails.py tests/test_video_first.py tests/test_evidence_before_ticket.py tests/test_evidence_only_what_was_seen.py
git commit -m "feat: every evidence ask is a video first, and three asks with nothing back hand over"
```

---

### Task 3: The prompts, the knowledge records and the contract ask for a video first

**Files:**
- Modify: `prompts/battery_support.md`
- Modify: `src/emotorad_ai/agents/motor_support.py` (`_BASE_PROMPT`)
- Modify: `src/emotorad_ai/agents/narrow_support.py` (`_RULES`)
- Modify: `knowledge/battery/arrival-damage.yaml`, `doodle-wont-power-on.yaml`, `impact-damage.yaml`, `melted-terminal-or-connector.yaml`, `wont-power-on.yaml`, `charging-port-damaged.yaml`, `onoff-switch-dead.yaml`
- Modify: `docs/contracts/amiigo-support-chat.md`, `CLAUDE.md`
- Refresh: `C:\Users\user\Desktop\Amiigo Support Chat API Contract.md` and `.pdf`
- Test: `tests/test_video_first_content.py` (new)

**Interfaces:**
- Consumes: nothing from Tasks 1 and 2 at run time.
- Produces: the texts below, exactly.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_video_first_content.py`:

```python
"""Video first in what the model reads (spec 2026-10-01-video-first-evidence-design.md)."""

import re
import unittest
from pathlib import Path

from emotorad_ai.agents import motor_support, narrow_support
from emotorad_ai.knowledge import load_records
from emotorad_ai.prompts import load_base_prompt

ROOT = Path(__file__).resolve().parents[1]
# A step telling the bot to get a photo from the customer.
ASKS_FOR_PHOTO = re.compile(
    r"(?<!not\s)(?<!never\s)\b(?:ask|asks|asking)\s+(?:the\s+customer\s+)?(?:for|to)\b[^.]{0,90}?"
    r"\b(?:photos?|photographs?|pictures?)\b"
    r"|\bcollect\b[^.]{0,40}?\bphotographs?\b"
    r"|\bwith\s+a\s+photo\b|\b(?:send|take)\s+a\s+photo\b|\bphotograph\s+the\b",
    re.IGNORECASE,
)
MEDIA = re.compile(r"\b(?:videos?|photos?|photographs?|pictures?)\b", re.IGNORECASE)


class KnowledgeTests(unittest.TestCase):
    def test_every_step_that_asks_for_a_photo_asks_for_a_video_first(self):
        offenders = []
        for record in load_records():
            for number, step in enumerate(record.steps, start=1):
                match = ASKS_FOR_PHOTO.search(step)
                if match is None:
                    continue
                first = MEDIA.search(step, match.start())
                if first is None or not first.group(0).lower().startswith("video"):
                    offenders.append("%s step %d" % (record.id, number))
        self.assertEqual(offenders, [])


class PromptTests(unittest.TestCase):
    def test_the_battery_prompt(self):
        text = load_base_prompt("battery_support")
        self.assertIn("**A short video first, a photo only if they can't.**", text)
        self.assertIn("they get a request for a video instead", text)
        self.assertIn("may ask for a short video", text)
        self.assertNotIn("close the chat gracefully", text)

    def test_the_motor_prompt(self):
        self.assertIn("ask for a short video of it", motor_support._BASE_PROMPT)

    def test_the_narrow_rules(self):
        self.assertIn("ask for a short video first, and a photo only if they cannot take one", narrow_support._RULES)

    def test_the_contract(self):
        contract = (ROOT / "docs" / "contracts" / "amiigo-support-chat.md").read_text(encoding="utf-8")
        self.assertIn("The bot asks for a short video first, and a photo if the rider can't take one.", contract)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="src;." python -m unittest tests.test_video_first_content`
Expected: FAIL on every test. `test_every_step_that_asks_for_a_photo_asks_for_a_video_first` lists exactly these ten offenders: battery-arrival-damage step 2, battery-charging-port-damaged step 2, battery-doodle-wont-power-on step 2, battery-impact-damage steps 3 and 7, battery-melted-terminal step 1, battery-onoff-switch-dead step 2, battery-wont-power-on steps 2, 6 and 17. If the list differs, compare it with the steps below before going on and record a ruling.

- [ ] **Step 3: The battery prompt**

In `prompts/battery_support.md` (lines end in ` \`; keep that style):

- Replace `all of these can be photographed in a few seconds, and \` with `all of these can be filmed in a few seconds, and \`.
- After the paragraph that ends `each one is a claim the rest of your diagnosis will rest on.`, insert a blank line and:

```
**A short video first, a photo only if they can't.** Ask for a short video of exactly \
what you need to see, for example "a short video of the charger plugged in, showing its \
light". A video shows what a photo misses: a light that blinks or changes colour, a \
sound, what happens when a button is pressed. Offer a photo only once the customer says \
they can't take a video, and from then on ask for photos. The platform adds a line asking \
for a video when you ask for a photo only, and hands the chat to a person after three \
asks with nothing back, so you never need to count.
```

- Replace `they get a request for a photo instead` with `they get a request for a video instead`.
- Replace the line `> - For **evidence (photo/video)**: tell the customer a ticket can't be raised without evidence, and close the chat gracefully — don't leave the case silently open.` with `> - For **evidence (a video, or a photo if they can't take one)**: do not count or close the chat yourself. After three asks with nothing back, the platform hands the chat to a person.`
- Replace `may ask for a photo or video — is that okay?` with `may ask for a short video — is that okay?`

- [ ] **Step 4: The motor prompt and the narrow rules**

In `src/emotorad_ai/agents/motor_support.py` `_BASE_PROMPT`, after the bullet that ends `Never suggest opening the motor, the controller or any wiring.`, insert:

```
- When you need to see something, ask for a short video of it, for example the wheel \
turning while the motor runs. Offer a photo only if the customer cannot take a video. Never \
ask for either in a safety case.
```

In `src/emotorad_ai/agents/narrow_support.py` `_RULES`, after the bullet that ends `has sent a photo or video of it.`, insert:

```
- When you need to see something, ask for a short video first, and a photo only if they cannot take one.
```

- [ ] **Step 5: The knowledge records**

Each replacement is exact text in the YAML (four-space indent inside a `>-` block; keep it).

`knowledge/battery/arrival-damage.yaml`:

```
    ASK WHETHER THE BIKE RUNS AND THE BATTERY CHARGES, and ask for photographs of the
    marks in the same message — every face that is affected, in good light.
```
becomes
```
    ASK WHETHER THE BIKE RUNS AND THE BATTERY CHARGES, and ask for a short video of the
    marks in the same message, turning slowly past every face that is affected, in good
    light. Photographs of each affected face only if a video is not possible.
```

`knowledge/battery/doodle-wont-power-on.yaml`:

```
    Ask the customer to connect the charger and report the LED — and to send a photo of
    it in the same message. The colour decides everything that follows, and a photo
    costs them five seconds. Ask for both together; asking for the picture afterwards
    means they have already put the phone down.
```
becomes
```
    Ask the customer to connect the charger and report the LED, and to send a short video
    of it in the same message, from the moment the charger goes in (a photo if they cannot
    take a video). The colour decides everything that follows, a video catches a red phase
    a photo can miss, and it costs them a few seconds. Ask for both together; asking for
    the video afterwards means they have already put the phone down.
```

`knowledge/battery/impact-damage.yaml`, step 3:

```
    collect it: photographs of the damaged area, and of the battery pack whether or not
    the pack is what looks damaged.
```
becomes
```
    collect it: a short video of the damaged area, then of the battery pack whether or not
    the pack is what looks damaged. Photographs of both only if a video is not possible;
    for a crack or a dent, ask them to hold the camera still on it for a second.
```

`knowledge/battery/impact-damage.yaml`, step 7:

```
    Collect the photographs and run the battery flow rather than the general one; a pack
```
becomes
```
    Collect the video (photographs if a video is not possible) and run the battery flow
    rather than the general one; a pack
```

`knowledge/battery/melted-terminal-or-connector.yaml`:

```
    ASK FOR TWO PHOTOS, AND ASK FOR THE SECOND ONE WHATEVER THE FIRST SHOWS. One end
    being clean says nothing about the other.
```
becomes
```
    ASK FOR TWO SHORT VIDEOS (TWO PHOTOS IF A VIDEO IS NOT POSSIBLE), AND ASK FOR THE
    SECOND ONE WHATEVER THE FIRST SHOWS. One end being clean says nothing about the
    other. Ask them to hold the camera still on the pins for a second.
```

`knowledge/battery/wont-power-on.yaml`, step 2:

```
    indicator light comes on. Ask for a photo of what they see in the same message; the
    answer decides everything that follows and a photo costs five seconds.
```
becomes
```
    indicator light comes on. Ask for a short video of what they see in the same message,
    from the moment they press the button (a photo if they cannot take a video); the
    answer decides everything that follows and a video costs a few seconds.
```

`knowledge/battery/wont-power-on.yaml`, step 6:

```
    report the LED colour, with a photo of it in the same message.
```
becomes
```
    report the LED colour, with a short video of it in the same message (a photo if they
    cannot take a video).
```

`knowledge/battery/wont-power-on.yaml`, step 17:

```
    unplug the charger, take the pack off the bike, and photograph the metal terminals,
```
becomes
```
    unplug the charger, take the pack off the bike, and take a short video of the metal
    terminals, holding the camera still on them for a second (photographs if a video is
    not possible),
```

`knowledge/battery/charging-port-damaged.yaml`:

```
    ASK FOR PHOTOGRAPHS AND A SHORT VIDEO TOGETHER, in one message. Photographs of the
    port itself, straight on and from the side so a bend shows; and a video of the charger
    actually being offered up to it, because "it does not hold" is a thing that happens in
    motion and a still picture cannot show it. Ask for both at once — asking for the video
    afterwards means they have already put the phone down.
```
becomes
```
    ASK FOR ONE SHORT VIDEO, in one message: the port itself, straight on and then from the
    side so a bend shows, then the charger actually being offered up to it, because "it
    does not hold" is a thing that happens in motion and a still picture cannot show it.
    Ask for it all at once; asking for the second part afterwards means they have already
    put the phone down. Photographs of the port, straight on and from the side, only if a
    video is not possible.
```

`knowledge/battery/onoff-switch-dead.yaml`:

```
    Ask for the pack from every side — photos of each face, top, bottom, both long sides
    and both ends — and a video of the switch actually being operated. Send the on/off
    switch guide picture with the request so they know which control you mean and what a
    working one looks like.
```
becomes
```
    Ask for one short video: the pack turned slowly to show every side (each face, top,
    bottom, both long sides and both ends), then the switch actually being operated.
    Photos of each side only if a video is not possible. Send the on/off switch guide
    picture with the request so they know which control you mean and what a working one
    looks like.
```

- [ ] **Step 6: The contract and CLAUDE.md**

In `docs/contracts/amiigo-support-chat.md`, section "Photos and videos", replace `A photo or video counts as the evidence the bot needs before it will raise a fault ticket.` with `A photo or video counts as the evidence the bot needs before it will raise a fault ticket. The bot asks for a short video first, and a photo if the rider can't take one.`

In `CLAUDE.md`, after the bullet that starts `- **Delete my data, self-service**`, add:

```markdown
- **Video first** (spec 2026-10-01): whenever the bot needs to see a fault it asks for a short video, and a photo only once the customer says they can't take one. The model decides when to ask; `evidence_asks.py` tells the runtime what a reply asked for. `Runtime._run` adds a line asking for a video to a photo-only ask (in Hindi for a Devanagari reply), counts asks in `ConversationState.evidence_asks` (the evidence post-check's included; reset when a video or photo reaches the model) and hands the chat to a person at the fourth ask with nothing back (`guardrail:evidence_not_forthcoming`). Safety reports never get an ask.
```

- [ ] **Step 7: Run the tests, then the whole suite**

Run: `PYTHONPATH="src;." python -m unittest tests.test_video_first_content tests.test_guardrails_and_knowledge tests.test_knowledge_migration`
Expected: OK.

Run: the Global Constraints test command. Expected: OK. A test that pinned an old record or prompt sentence changed above is updated to the new sentence, each as a ruling.

- [ ] **Step 8: Refresh the Desktop copies**

```bash
cp docs/contracts/amiigo-support-chat.md "/c/Users/user/Desktop/Amiigo Support Chat API Contract.md"
SP="C:/Users/user/AppData/Local/Temp/claude/E--irctc-test/9594a7d0-9e01-461a-848c-e6222b53eaa4/scratchpad"
python "$SP/md_to_html.py" "$SP/contract.html"
"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="C:/Users/user/Desktop/Amiigo Support Chat API Contract.pdf" "file:///$SP/contract.html"
python -c "import pathlib; d = pathlib.Path(r'C:/Users/user/Desktop/Amiigo Support Chat API Contract.pdf').read_bytes(); print(d[:5])"
grep -c "The bot asks for a short video first" "$SP/contract.html"
```

Expected: `b'%PDF-'`, and `1`.

- [ ] **Step 9: Commit**

```bash
git add prompts/battery_support.md src/emotorad_ai/agents/motor_support.py src/emotorad_ai/agents/narrow_support.py knowledge/battery/ docs/contracts/amiigo-support-chat.md CLAUDE.md tests/test_video_first_content.py
git commit -m "docs: prompts, knowledge records and the contract ask for a video first"
```
