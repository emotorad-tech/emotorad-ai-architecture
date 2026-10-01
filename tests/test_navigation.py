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
