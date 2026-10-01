"""Video first in the runtime (spec 2026-10-01-video-first-evidence-design.md)."""

import unittest

from emotorad_ai.agents.base import HANDOVER_TEXT
from emotorad_ai.agents.late_warranty import AGENT_NAME as LATE_WARRANTY
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


HAZARD = ("Please stop using the bike and stop charging the battery now; a bulge in the casing can be a hazard. "
          "Could you send a photo of the side of the pack so our team can see it?")


class FinalReviewRuntimeTests(unittest.TestCase):
    """The final review (2026-10-01)."""

    def test_a_stop_instruction_is_never_replaced_by_the_hand_over(self):
        rt = runtime([say(VIDEO_ASK)] * 3 + [say(HAZARD)], media_store=Store())
        for text in ("my battery isn't charging", "hmm", "ok"):
            send(rt, text)
        reply = send(rt, "the battery case looks a bit uneven on one side")
        self.assertIn("stop charging", reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)

    def test_a_stop_instruction_gets_no_video_line_and_no_count(self):
        rt = runtime([say(HAZARD)], media_store=Store())
        reply = send(rt, "the battery case looks a bit uneven on one side")
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)
        self.assertEqual(state(rt).evidence_asks, 0)

    def test_only_the_fault_agents_ask_for_a_video_first(self):
        rt = runtime([say("Could you send a photo of the bike?")], media_store=Store())
        state(rt).route_to(LATE_WARRANTY)
        reply = send(rt, "I bought it in 2023")
        self.assertNotIn(VIDEO_FIRST_LINE, reply.text)
        self.assertEqual(state(rt).evidence_asks, 0)

    def test_after_a_hand_over_the_count_starts_again(self):
        rt = runtime([say(VIDEO_ASK)] * 5, media_store=Store())
        for text in ("my battery isn't charging", "hmm", "ok"):
            send(rt, text)
        self.assertIn(HANDOVER_TEXT, send(rt, "what now").text)
        fifth = send(rt, "ok, when will they call?")
        self.assertNotIn(HANDOVER_TEXT, fifth.text)
        self.assertIn(VIDEO_ASK, fifth.text)
        self.assertEqual(state(rt).evidence_asks, 1)
