import unittest

from emotorad_ai.conversation import ConversationState
from emotorad_ai.disclosure import (
    DISCLOSURE_TEXT,
    DISCLOSURE_VOICE,
    apply_disclosure,
    has_disclosure,
)


class DisclosureTests(unittest.TestCase):
    def test_the_first_reply_of_a_conversation_carries_it(self):
        state = ConversationState("c1")
        reply = apply_disclosure("How can I help?", state, "whatsapp")
        self.assertTrue(reply.startswith(DISCLOSURE_TEXT))
        self.assertTrue(state.disclosed)

    def test_it_is_said_once_per_conversation_not_once_per_turn(self):
        state = ConversationState("c1")
        apply_disclosure("How can I help?", state, "whatsapp")
        second = apply_disclosure("Try a different socket.", state, "whatsapp")
        self.assertEqual(second, "Try a different socket.")

    def test_voice_gets_wording_written_to_be_read_aloud(self):
        state = ConversationState("c1")
        reply = apply_disclosure("How can I help?", state, "voice")
        self.assertTrue(reply.startswith(DISCLOSURE_VOICE))
        self.assertNotIn("—", reply, "an em dash becomes a pause in the wrong place")

    def test_a_reply_that_already_discloses_is_not_doubled_up(self):
        state = ConversationState("c1")
        reply = apply_disclosure(
            "I'm an AI assistant and I can help with that.", state, "website_chat"
        )
        self.assertEqual(reply.count("AI"), 1)
        self.assertTrue(state.disclosed)

    def test_every_channel_discloses(self):
        for channel in ("website_chat", "whatsapp", "voice", "amiigo_app"):
            state = ConversationState("c-%s" % channel)
            self.assertTrue(
                has_disclosure(apply_disclosure("Hello.", state, channel)), channel
            )


class DetectionTests(unittest.TestCase):
    def test_reasonable_rephrasings_are_recognised(self):
        # Used to verify model-written text in evals, so it must not only match
        # our own exact string.
        for text in (
            "I'm an AI assistant.",
            "You are speaking to an automated assistant.",
            "I am a bot, not a person.",
            "This is a virtual assistant.",
            "I'm not a real person, but I can help.",
        ):
            self.assertTrue(has_disclosure(text), text)

    def test_ordinary_replies_are_not_mistaken_for_disclosure(self):
        for text in (
            "Try a different wall socket.",
            "Your battery is under warranty.",
            "",
        ):
            self.assertFalse(has_disclosure(text), text)


if __name__ == "__main__":
    unittest.main()
