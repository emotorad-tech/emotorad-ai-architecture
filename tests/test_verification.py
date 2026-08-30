"""Identity verification: the gate that turns an anonymous chat into a verified one.

The property under test throughout is that **the model decides nothing here**.
It relays a number and relays a code; code compares them. Every test below is a
way of asking whether a model — or a customer talking to one — could get past
that by being persuasive.
"""

import unittest

from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.tools.verification import (
    MAX_ATTEMPTS,
    REQUEST_IDENTITY_VERIFICATION,
    VERIFY_IDENTITY,
    VerificationStore,
)

CODE = "424242"


class VerificationToolTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.registry = build_registry(verification=self.store)
        self.ctx = ToolContext(conversation_id="c1")

    def _request(self, phone="+919876543210", ctx=None):
        return self.registry.call(REQUEST_IDENTITY_VERIFICATION, {"phone": phone}, ctx or self.ctx)

    def _verify(self, code, ctx=None):
        return self.registry.call(VERIFY_IDENTITY, {"code": code}, ctx or self.ctx)

    def test_the_happy_path_verifies_the_number(self):
        self._request()
        code = self.store.pending_code("c1")
        self.assertFalse(is_error(self._verify(code)))
        self.assertEqual(self.store.verified_phone("c1"), "+919876543210")

    def test_nothing_is_verified_before_the_right_code_arrives(self):
        self._request()
        self.assertIsNone(self.store.verified_phone("c1"))
        self.assertTrue(is_error(self._verify("000000")))
        self.assertIsNone(self.store.verified_phone("c1"))

    def test_the_code_is_never_returned_to_the_model(self):
        # The tool result is what lands in the transcript the model reads. If the
        # code appeared there, the model could simply tell the customer.
        envelope = self._request()
        self.assertNotIn(self.store.pending_code("c1"), str(envelope))
        self.assertEqual(envelope["data"], {"sent": True, "phone_ending": "3210"})

    def test_requesting_a_code_reveals_nothing_about_who_is_registered(self):
        # Identical answers for a registered and an unregistered number, so the
        # flow cannot be used to enumerate EMotorad owners.
        registered = self._request("+919876543210")
        unknown = self._request("+919000000123", ctx=ToolContext(conversation_id="c2"))
        self.assertEqual(registered["data"].keys(), unknown["data"].keys())
        self.assertEqual(registered["data"]["sent"], unknown["data"]["sent"])

    def test_a_malformed_number_is_refused_without_sending_anything(self):
        envelope = self._request("12345")
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "invalid_phone")
        self.assertIsNone(self.store.pending_code("c1"))

    def test_attempts_are_capped_and_locking_hands_over(self):
        self._request()
        for _ in range(MAX_ATTEMPTS):
            self.assertTrue(is_error(self._verify("000000")))
        locked = self._verify(self.store.pending_code("c1") or "000000")
        self.assertEqual(locked["error"]["code"], "verification_locked")
        self.assertEqual(locked["error"]["remedy"], "human_handoff")
        self.assertIsNone(self.store.verified_phone("c1"))

    def test_asking_for_a_new_code_does_not_buy_more_guesses(self):
        # Otherwise the cap is decorative: burn four attempts, request again,
        # repeat forever.
        self._request()
        for _ in range(MAX_ATTEMPTS):
            self._verify("000000")
        self._request()
        self.assertTrue(is_error(self._verify("000000")))
        self.assertEqual(self.store.attempts_left("c1"), 0)

    def test_verification_does_not_leak_across_conversations(self):
        self._request()
        code = self.store.pending_code("c1")
        other = ToolContext(conversation_id="c2")
        self.assertTrue(is_error(self._verify(code, ctx=other)))
        self.assertIsNone(self.store.verified_phone("c2"))

    def test_the_conversation_id_is_injected_not_model_supplied(self):
        # A model that could name the conversation could verify itself into
        # someone else's session.
        for name in (REQUEST_IDENTITY_VERIFICATION, VERIFY_IDENTITY):
            spec = self.registry.specs[name]
            self.assertIn("conversation_id", spec.injects)
            self.assertNotIn("conversation_id", spec.schema()["input_schema"]["properties"])

    def test_the_tools_are_absent_unless_a_store_is_wired(self):
        # An agent told it can verify people, that then cannot, will keep trying.
        registry = build_registry()
        self.assertNotIn(REQUEST_IDENTITY_VERIFICATION, registry.specs)
        self.assertNotIn(VERIFY_IDENTITY, registry.specs)


if __name__ == "__main__":
    unittest.main()
