import unittest

from emotorad_ai.adapters import AmiigoAdapter, VoiceAdapter, WhatsAppAdapter
from emotorad_ai.adapters.whatsapp import extract_ref, strip_ref
from emotorad_ai.contract import ANONYMOUS, ASSERTED, VERIFIED
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.tools.mocks import build_registry


def resolver():
    return IdentityResolver(build_registry())


class RefCodeTests(unittest.TestCase):
    def test_the_code_is_found_in_the_shapes_customers_actually_send(self):
        for text in (
            "Hi, I'd like to know more. [ref:7KQ2M9]",
            "hello [REF: 7kq2m9]",
            "ref=7KQ2M9 my battery is dead",
        ):
            self.assertEqual(extract_ref(text), "7KQ2M9", text)

    def test_no_code_is_not_an_error(self):
        self.assertIsNone(extract_ref("hi, my battery won't charge"))
        self.assertIsNone(extract_ref(""))

    def test_the_code_is_stripped_before_the_model_sees_the_text(self):
        # It is plumbing. Left in, the model comments on it or echoes it back.
        self.assertEqual(
            strip_ref("Hi, I'd like to know more. [ref:7KQ2M9]"), "Hi, I'd like to know more."
        )


class WhatsAppTests(unittest.TestCase):
    def test_the_sender_is_verified_without_an_otp(self):
        message = WhatsAppAdapter(resolver()).to_message(
            {"from": "919876543210", "text": "battery won't charge"}
        )
        self.assertEqual(message.persona, "customer")
        self.assertEqual(message.identity.strength, VERIFIED)
        self.assertEqual(message.identity.phone, "+919876543210")
        self.assertTrue(message.identity.may_disclose)

    def test_a_ref_code_carries_the_browsing_history_into_the_conversation(self):
        # The highest-value stitch in the identity design: an anonymous browser
        # becomes a verified phone with history already attached.
        shared = resolver()
        _, browser = shared.resolve_website("cookie-laptop", session_token=None)

        adapter = WhatsAppAdapter(shared, ref_lookup=lambda code: "cookie-laptop")
        message = adapter.to_message(
            {"from": "919876543210", "text": "Hi there [ref:7KQ2M9]"}
        )

        self.assertEqual(message.identity.cluster_id, browser.cluster_id)
        self.assertTrue(message.entry_metadata["ref_resolved"])
        self.assertEqual(message.message_text, "Hi there")

    def test_an_expired_code_degrades_to_phone_only_rather_than_failing(self):
        # Codes have a TTL. A miss is normal and must not break the conversation.
        adapter = WhatsAppAdapter(resolver(), ref_lookup=lambda code: None)
        message = adapter.to_message({"from": "919876543210", "text": "hi [ref:EXPIRED1]"})
        self.assertEqual(message.identity.strength, VERIFIED)
        self.assertFalse(message.entry_metadata["ref_resolved"])

    def test_a_tapped_template_button_is_the_intent(self):
        message = WhatsAppAdapter(resolver()).to_message(
            {"from": "919876543210", "text": "", "template_reply": "battery"}
        )
        self.assertEqual(message.pill_clicked, "battery")

    def test_attachments_survive(self):
        message = WhatsAppAdapter(resolver()).to_message(
            {
                "from": "919876543210",
                "text": "here is my invoice",
                "attachments": [{"kind": "image", "url": "https://x.test/i.jpg"}],
            }
        )
        self.assertEqual(len(message.attachments), 1)


class VoiceTests(unittest.TestCase):
    def test_caller_id_is_asserted_and_authorises_nothing(self):
        message = VoiceAdapter(resolver()).to_message(
            {"caller_id": "+919876543210", "transcript": "my battery is dead", "confidence": 0.95}
        )
        self.assertEqual(message.identity.strength, ASSERTED)
        self.assertFalse(message.identity.may_disclose, "caller ID is spoofable")

    def test_a_withheld_number_is_unknown_not_a_crash(self):
        message = VoiceAdapter(resolver()).to_message({"transcript": "hello"})
        self.assertEqual(message.persona, "unknown")
        self.assertEqual(message.identity.strength, ANONYMOUS)

    def test_a_low_confidence_transcript_is_flagged_not_dropped(self):
        message = VoiceAdapter(resolver()).to_message(
            {"caller_id": "+919876543210", "transcript": "my bat rees dad", "confidence": 0.3}
        )
        self.assertTrue(message.entry_metadata["low_confidence_transcript"])
        self.assertEqual(message.message_text, "my bat rees dad")

    def test_a_confident_transcript_is_not_flagged(self):
        message = VoiceAdapter(resolver()).to_message(
            {"caller_id": "+919876543210", "transcript": "battery dead", "confidence": 0.9}
        )
        self.assertNotIn("low_confidence_transcript", message.entry_metadata)

    def test_a_keypad_press_beats_the_transcript(self):
        message = VoiceAdapter(resolver()).to_message(
            {"caller_id": "+919876543210", "transcript": "errr", "dtmf": "1"}
        )
        self.assertEqual(message.pill_clicked, "1")

    def test_the_call_id_is_the_conversation_id(self):
        message = VoiceAdapter(resolver()).to_message(
            {"caller_id": "+919876543210", "transcript": "hi", "call_id": "CALL-77"}
        )
        self.assertEqual(message.conversation_id, "CALL-77")


class AmiigoTests(unittest.TestCase):
    def test_a_logged_in_app_session_is_verified(self):
        message = AmiigoAdapter(resolver()).to_message(
            {"session_token": "sess-ananya", "text": "battery issue"}
        )
        self.assertEqual(message.identity.strength, VERIFIED)
        self.assertEqual(message.identity.phone, "+919876543210")

    def test_the_screen_they_tapped_from_is_kept_as_intent_evidence(self):
        message = AmiigoAdapter(resolver()).to_message(
            {"session_token": "sess-ananya", "text": "help", "screen": "battery_health"}
        )
        self.assertEqual(message.entry_metadata["screen"], "battery_health")

    def test_an_unknown_session_does_not_become_a_verified_customer(self):
        message = AmiigoAdapter(resolver()).to_message(
            {"session_token": "sess-nope", "text": "hi"}
        )
        self.assertNotEqual(message.identity.strength, VERIFIED)


class AllChannelsTests(unittest.TestCase):
    def test_every_adapter_emits_the_same_contract(self):
        shared = resolver()
        messages = [
            WhatsAppAdapter(shared).to_message({"from": "919876543210", "text": "hi"}),
            VoiceAdapter(shared).to_message({"caller_id": "+919876543210", "transcript": "hi"}),
            AmiigoAdapter(shared).to_message({"session_token": "sess-ananya", "text": "hi"}),
        ]
        for message in messages:
            self.assertTrue(message.conversation_id)
            self.assertIn(message.persona, ("customer", "unknown"))
            self.assertIsNotNone(message.identity.strength)
            self.assertIs(message.about, message.identity, "no subject outside internal")


if __name__ == "__main__":
    unittest.main()
