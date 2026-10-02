"""The final review of going back (2026-10-02): everyday troubleshooting
replies that were read as navigation, and the wrong-code counter."""

import unittest

from emotorad_ai.llm import say
from emotorad_ai.navigation import names_the_mobile, wants_change_bike, wants_change_number, wants_start_over
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.triage import ASK_RIGHT_FRAME
from tests.test_navigation_chat import CHECK, troubleshooting
from tests.test_verify_first import RIDER, Chat


class PhraseTests(unittest.TestCase):
    def test_agreeing_to_restart_is_not_starting_over(self):
        for text in ("ok restart", "let me restart", "ok let me restart", "i need to restart", "please restart",
                     "can i restart?"):
            self.assertFalse(wants_start_over(text), text)
        for text in ("restart", "Restart.", "restart the chat", "can I restart the conversation?", "start over"):
            self.assertTrue(wants_start_over(text), text)

    def test_correct_number_is_a_yes(self):
        for text in ("Correct number.", "correct number", "ok correct no", "fix number", "edit number"):
            self.assertFalse(wants_change_number(text), text)
        self.assertTrue(wants_change_number("update my number"))

    def test_an_echoed_question_is_not_a_change_of_bike(self):
        for text in ("a different one?", "a different one", "wrong one", "other bike?", "not this one?"):
            self.assertFalse(wants_change_bike(text), text)
        for text in ("not this one", "other bike", "it's my other bike", "wrong bike"):
            self.assertTrue(wants_change_bike(text), text)

    def test_my_number_is_the_mobile(self):
        self.assertTrue(names_the_mobile("change my number"))
        self.assertTrue(names_the_mobile("that's not my number"))
        self.assertFalse(names_the_mobile("wrong number"))


class TurnTests(unittest.TestCase):
    def test_let_me_restart_keeps_the_troubleshooting(self):
        chat = Chat(replies=[CHECK, say("Does the display come on now?")])
        troubleshooting(chat)
        reply = chat.say("ok let me restart")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(chat.state().pending_topic, None)
        self.assertIsNotNone(chat.state().selected_frame)

    def test_wrong_number_while_giving_a_frame_number_keeps_the_verification(self):
        chat = Chat()
        chat.verify()
        chat.say("none of these")
        chat.say("EMXP2026009999")
        reply = chat.say("sorry wrong number")
        self.assertEqual((reply.handled_by, reply.text), ("triage", ASK_RIGHT_FRAME))
        self.assertEqual(chat.store.verified_phone("c1"), RIDER)
        self.assertIsNone(chat.state().unlisted_bike["frame_number"])

    def test_wrong_number_mid_troubleshooting_is_for_the_agent(self):
        chat = Chat(replies=[CHECK, say("No problem, which number should I note?")])
        troubleshooting(chat)
        self.assertEqual(chat.say("sorry wrong number").handled_by, "battery_support")
        self.assertEqual(chat.store.verified_phone("c1"), RIDER)

    def test_correct_number_mid_troubleshooting_is_for_the_agent(self):
        chat = Chat(replies=[CHECK, say("Thanks.")])
        troubleshooting(chat)
        self.assertEqual(chat.say("Correct number.").handled_by, "battery_support")
        self.assertEqual(chat.store.verified_phone("c1"), RIDER)

    def test_change_my_mobile_number_mid_troubleshooting_still_works(self):
        chat = Chat(replies=[CHECK])
        troubleshooting(chat)
        self.assertEqual(chat.say("change my mobile number").handled_by, "verify_first:change_number")

    def test_a_different_one_echoed_keeps_the_bike(self):
        chat = Chat(replies=[CHECK, say("Yes, try a different socket.")])
        troubleshooting(chat)
        self.assertEqual(chat.say("a different one?").handled_by, "battery_support")
        self.assertIsNotNone(chat.state().selected_frame)


class WrongCodeCounterTests(unittest.TestCase):
    def wrong(self, chat):
        return "000000" if chat.code() != "000000" else "111111"

    def test_change_number_does_not_give_more_guesses(self):
        chat = Chat()
        chat.say("my battery isn't charging")
        chat.say(RIDER[3:])
        for _ in range(4):
            chat.say(self.wrong(chat))
        self.assertEqual(chat.store.attempts_left("c1"), 1)
        self.assertEqual(chat.say("change number").handled_by, "verify_first:change_number")
        self.assertIsNone(chat.code())
        chat.say(RIDER[3:])
        self.assertEqual(chat.store.attempts_left("c1"), 1)

    def test_a_cancelled_code_cannot_be_used(self):
        store = VerificationStore()
        store.issue("c1", "+919700000010", "123456")
        store.cancel_code("c1")
        self.assertFalse(store.check("c1", "123456"))
        self.assertIsNone(store.pending_code("c1"))
        self.assertIsNone(store.pending_phone("c1"))
        self.assertEqual(store.attempts_left("c1"), 5)


if __name__ == "__main__":
    unittest.main()
