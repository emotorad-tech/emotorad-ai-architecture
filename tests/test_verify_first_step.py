"""The verify-first step on its own, without the runtime around it."""

import unittest
from datetime import date

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Attachment, Identity, InboundMessage
from emotorad_ai.conversation import AWAITING_BIKE_SELECTION, ConversationState
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.observability import EventLog
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.navigation import GREETING_TEXT
from emotorad_ai.verify_first import ASK_NUMBER, CHANGE_NUMBER, PHOTO_SAFETY, VerifyFirst

TODAY = date(2026, 9, 30)


def message(text, photo=False):
    attachments = [Attachment(kind="image", url="data:image/jpeg;base64,AAAA")] if photo else []
    return InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
                          identity=Identity(strength=ANONYMOUS, em_aid="aid-1"), attachments=attachments)


class StepTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.registry = build_registry(verification=self.store, today=TODAY,
                                       account_finder=fixtures.find_account_by_order_code)
        self.log = EventLog(path=None)
        self.step = VerifyFirst(self.registry, IdentityResolver(self.registry), self.log)
        self.state = ConversationState("c1")

    def test_first_contact_asks_for_the_number_and_keeps_the_topic(self):
        reply = self.step.handle(message("my battery isn't charging"), self.state)
        self.assertEqual(reply.outcome, "ask_number")
        self.assertEqual(reply.text, ASK_NUMBER)
        self.assertEqual(self.state.verify_step, "number")
        self.assertEqual(self.state.pending_topic, "battery")

    def test_a_greeting_is_greeted_back_and_the_step_waits(self):
        reply = self.step.handle(message("hi"), self.state)
        self.assertEqual((reply.outcome, reply.text), ("greeting", GREETING_TEXT))
        self.assertIsNone(self.state.verify_step)

    def test_change_number_at_the_code_step_cancels_the_code(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.step.handle(message("9700000010"), self.state)
        reply = self.step.handle(message("wrong number"), self.state)
        self.assertEqual((reply.outcome, reply.text), ("change_number", CHANGE_NUMBER))
        self.assertIsNone(self.store.pending_code("c1"))
        self.assertEqual(self.state.verify_step, "number")

    def test_change_number_at_the_number_step(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.assertEqual(self.step.handle(message("change number"), self.state).outcome, "change_number")

    def test_resend_is_not_change_number(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.step.handle(message("9700000010"), self.state)
        self.assertEqual(self.step.handle(message("please resend"), self.state).outcome, "code_resent")

    def test_a_photo_adds_the_safety_line(self):
        reply = self.step.handle(message("is this normal?", photo=True), self.state)
        self.assertEqual(reply.text, ASK_NUMBER + " " + PHOTO_SAFETY)

    def test_a_number_sends_a_code_and_the_model_text_hides_it(self):
        self.step.handle(message("hi"), self.state)
        reply = self.step.handle(message("it's 97000 00010"), self.state)
        self.assertEqual(reply.outcome, "code_sent")
        self.assertIn("•••••••010", reply.text)
        self.assertEqual(reply.model_text, "it's [phone]")
        self.assertEqual(self.state.verify_step, "code")
        self.assertIsNotNone(self.store.pending_code("c1"))

    def test_the_right_code_returns_the_bikes(self):
        self.step.handle(message("hi"), self.state)
        self.step.handle(message("9700000010"), self.state)
        code = self.store.pending_code("c1")
        reply = self.step.handle(message(code), self.state)
        self.assertEqual(reply.outcome, "verified")
        self.assertEqual(reply.model_text, "[code]")
        self.assertEqual([b["frame_number"] for b in reply.resolved.bikes], ["EMXP2026001234", "DDL32023045678"])
        self.assertTrue(reply.text.startswith("Thanks, that's confirmed. I found 2 bikes on this number:"))
        self.assertEqual(self.state.phase, AWAITING_BIKE_SELECTION)
        self.assertIsNone(self.state.verify_step)
        self.assertIsNone(self.state.context_block)

    def test_each_outcome_is_logged_without_the_number_or_the_code(self):
        self.step.handle(message("my battery isn't charging"), self.state)
        self.step.handle(message("9700000010"), self.state)
        code = self.store.pending_code("c1")
        self.step.handle(message(code), self.state)
        events = [e for e in self.log.events if e["event"] == "verify_first"]
        self.assertEqual([e["outcome"] for e in events], ["ask_number", "code_sent", "verified"])
        self.assertNotIn("9700000010", repr(events))
        self.assertNotIn(code, repr(events))

    def test_the_tools_it_calls_are_logged_as_tool_calls(self):
        # So a save conflict sees the side effect and never runs the turn again.
        self.step.handle(message("hi"), self.state)
        self.step.handle(message("9700000010"), self.state)
        tools = [e["tool"] for e in self.log.events if e["event"] == "tool_call"]
        self.assertIn("request_identity_verification", tools)


class AppliesTests(unittest.TestCase):
    def setUp(self):
        registry = build_registry(verification=VerificationStore())
        self.step = VerifyFirst(registry, IdentityResolver(registry), EventLog(path=None))

    def test_an_anonymous_customer(self):
        self.assertTrue(self.step.applies(ResolvedIdentity(persona="customer", method="unverified",
                                                           identity=Identity(strength=ANONYMOUS, em_aid="a"))))

    def test_not_a_verified_customer_or_a_dealer(self):
        self.assertFalse(self.step.applies(ResolvedIdentity(persona="customer", method="verified",
                                                            identity=Identity(strength=VERIFIED, phone="+919700000010"))))
        self.assertFalse(self.step.applies(ResolvedIdentity(persona="dealer", method="verified",
                                                            identity=Identity(strength=VERIFIED, phone="+919800000001"))))

    def test_not_without_the_verification_tools(self):
        registry = build_registry()
        step = VerifyFirst(registry, IdentityResolver(registry), EventLog(path=None))
        self.assertFalse(step.applies(ResolvedIdentity(persona="customer", method="unverified",
                                                       identity=Identity(strength=ANONYMOUS, em_aid="a"))))


if __name__ == "__main__":
    unittest.main()
