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
