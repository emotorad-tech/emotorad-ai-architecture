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
