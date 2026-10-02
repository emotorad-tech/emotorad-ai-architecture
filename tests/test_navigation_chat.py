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
        self.assertEqual(chat.say("wrong mobile number").handled_by, "verify_first:change_number")
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
