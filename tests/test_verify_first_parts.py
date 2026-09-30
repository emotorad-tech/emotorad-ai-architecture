"""The pieces the verify-first step is built from (spec 2026-09-30).

Each is small and has a use outside the step too: the transcript and the log
hide a number however it was typed, a code can be sent again to the number it
went to, the code sender is a stand-in that says what it did, and the order
number fallback can be tried without the OMS key.
"""

import itertools
import unittest

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage
from emotorad_ai.observability import redact_pii
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext, ToolRegistry
from emotorad_ai.tools.verification import (
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    MockOtpSender,
    VerificationStore,
    apply_proven_phone,
    register_verification_tools,
)


class RedactionTests(unittest.TestCase):
    def test_a_number_typed_in_groups_or_with_a_leading_zero_is_hidden(self):
        for typed in ("97000 00010", "97000-00010", "09700000010", "+91 97000-00010", "+91-9700000010"):
            self.assertNotIn("00010", redact_pii("call me on " + typed), typed)

    def test_the_forms_already_hidden_still_are(self):
        self.assertEqual(redact_pii("call me on 9876500000"), "call me on [phone]")
        self.assertEqual(redact_pii("+919876500000"), "[phone]")

    def test_other_numbers_are_left_alone(self):
        for text in ("1500 km and the range has dropped by half", "it shows E-06", "48V 14.4Ah removable",
                     "frame EMXP2026001234"):
            self.assertEqual(redact_pii(text), text)


def registry_with(store, codes=("111111", "222222", "333333")):
    # A bare registry (build_registry would register the same tools first, and
    # a second registration raises), with a fixed sequence of codes so a resend
    # can be told apart from the first.
    registry = ToolRegistry()
    register_verification_tools(registry, store, code_factory=itertools.cycle(codes).__next__)
    return registry


class ResendTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.registry = registry_with(self.store)
        self.ctx = ToolContext(conversation_id="c1")

    def call(self, name, arguments):
        return self.registry.call(name, arguments, self.ctx)

    def test_a_code_can_be_sent_again_to_the_number_it_went_to(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"})
        again = self.call(REQUEST_IDENTITY_VERIFICATION, {})
        self.assertEqual(again["data"]["phone_masked"], "•••••••010")
        self.assertEqual(self.store.pending_code("c1"), "222222")

    def test_with_nothing_pending_it_still_refuses(self):
        self.assertEqual(self.call(REQUEST_IDENTITY_VERIFICATION, {})["error"]["code"], "no_number_on_file")

    def test_a_proved_number_is_not_pending_any_more(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"})
        self.call(VERIFY_IDENTITY, {"code": "111111"})
        self.assertIsNone(self.store.pending_phone("c1"))
        self.assertEqual(self.call(REQUEST_IDENTITY_VERIFICATION, {})["error"]["code"], "no_number_on_file")

    def test_the_pending_number_is_the_full_one_for_the_store_only(self):
        self.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "97000 00010"})
        self.assertEqual(self.store.pending_phone("c1"), "+919700000010")


class MockOtpSenderTests(unittest.TestCase):
    def test_it_logs_the_masked_number_and_never_the_code(self):
        sender = MockOtpSender()
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO") as logs:
            sender("9700000010", "482913")
        said = "\n".join(logs.output)
        self.assertIn("•••••••010", said)
        self.assertNotIn("482913", said)
        self.assertNotIn("9700000010", said)
        self.assertEqual(sender.sent, ["•••••••010"])

    def test_the_registry_calls_it_when_a_code_is_sent(self):
        sender = MockOtpSender()
        registry = build_registry(verification=VerificationStore(), send_code=sender)
        with self.assertLogs("emotorad_ai.tools.verification", level="INFO"):
            registry.call(REQUEST_IDENTITY_VERIFICATION, {"phone": "9700000010"}, ToolContext(conversation_id="c1"))
        self.assertEqual(sender.sent, ["•••••••010"])


def anonymous(phone=None):
    return InboundMessage(conversation_id="c1", persona="customer", channel="website_chat", message_text="hi",
                          identity=Identity(strength=ANONYMOUS, em_aid="aid-1", phone=phone))


class ApplyProvenPhoneTests(unittest.TestCase):
    def test_it_opens_an_anonymous_identity(self):
        opened = apply_proven_phone(anonymous(), "+919700000010")
        self.assertEqual(opened.identity.strength, VERIFIED)
        self.assertEqual(opened.identity.phone, "+919700000010")

    def test_it_never_overwrites_a_phone_already_there(self):
        message = anonymous(phone="+919876543210")
        self.assertIs(apply_proven_phone(message, "+919700000010"), message)

    def test_no_proof_changes_nothing(self):
        message = anonymous()
        self.assertIs(apply_proven_phone(message, None), message)


class FixtureOrderCodesTests(unittest.TestCase):
    def test_an_order_and_an_invoice_number_find_the_test_rider(self):
        self.assertEqual(fixtures.find_account_by_order_code("EMO-100234"), fixtures.PHONE_AMIIGO_TEST_RIDER)
        self.assertEqual(fixtures.find_account_by_order_code(" inv-2026-0042 "), fixtures.PHONE_AMIIGO_TEST_RIDER)

    def test_an_unknown_number_finds_nobody(self):
        self.assertIsNone(fixtures.find_account_by_order_code("EMO-999999"))
        self.assertIsNone(fixtures.find_account_by_order_code(""))


if __name__ == "__main__":
    unittest.main()
