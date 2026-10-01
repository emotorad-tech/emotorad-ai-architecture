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
