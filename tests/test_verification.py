"""Identity verification: the gate that turns an anonymous chat into a verified one.

The property under test throughout is that **the model decides nothing here**.
It relays a number and relays a code; code compares them. Every test below is a
way of asking whether a model — or a customer talking to one — could get past
that by being persuasive.
"""

import unittest

from emotorad_ai.tools.mocks import RAISE_INTAKE_TICKET, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.tools.verification import (
    FIND_ACCOUNT_BY_CODE,
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
        self.assertEqual(envelope["data"], {"sent": True, "phone_masked": "•••••••210"})

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


class AccountRecoveryTests(unittest.TestCase):
    """An order code finds an account. It must never open one."""

    def setUp(self):
        self.store = VerificationStore()
        self.registry = build_registry(
            verification=self.store,
            account_finder=lambda code: "+919000000055" if code in ("ORD/1", "INV/1") else None,
        )
        self.ctx = ToolContext(conversation_id="c1")

    def _find(self, code, ctx=None):
        return self.registry.call(FIND_ACCOUNT_BY_CODE, {"code": code}, ctx or self.ctx)

    def test_a_code_returns_only_a_masked_number(self):
        data = self._find("ORD/1")["data"]
        self.assertEqual(data, {"found": True, "phone_masked": "•••••••055"})

    def test_the_full_number_never_reaches_the_model(self):
        # The whole point: an invoice is printed on paper and emailed around, so
        # if this handed back a phone number, a leaked invoice would be a leaked
        # contact detail.
        envelope = self._find("ORD/1")
        self.assertNotIn("9000000055", str(envelope))
        self.assertNotIn("600515", str(envelope))

    def test_no_name_address_or_bike_comes_back(self):
        data = self._find("ORD/1")["data"]
        self.assertEqual(set(data), {"found", "phone_masked"})

    def test_both_phone_formats_mask_identically(self):
        # One number must not present as two different masks depending on which
        # tool the customer happened to reach it through.
        from emotorad_ai.tools.verification import mask_phone

        self.assertEqual(mask_phone("+919000000055"), mask_phone("9000000055"))

    def test_an_unknown_code_is_refused(self):
        envelope = self._find("ORD/nope")
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "order_not_found")

    def test_finding_an_account_verifies_nothing_on_its_own(self):
        self._find("ORD/1")
        self.assertIsNone(self.store.verified_phone("c1"))

    def test_the_code_goes_to_the_recovered_number_without_the_model_naming_it(self):
        self._find("ORD/1")
        envelope = self.registry.call(REQUEST_IDENTITY_VERIFICATION, {}, self.ctx)
        self.assertFalse(is_error(envelope))
        self.assertEqual(envelope["data"]["phone_masked"], "•••••••055")
        code = self.store.pending_code("c1")
        self.registry.call(VERIFY_IDENTITY, {"code": code}, self.ctx)
        self.assertEqual(self.store.verified_phone("c1"), "+919000000055")

    def test_a_candidate_from_one_conversation_is_not_available_to_another(self):
        self._find("ORD/1")
        envelope = self.registry.call(
            REQUEST_IDENTITY_VERIFICATION, {}, ToolContext(conversation_id="c2")
        )
        self.assertEqual(envelope["error"]["code"], "no_number_on_file")

    def test_asking_for_a_code_with_nothing_on_file_fails_clearly(self):
        envelope = self.registry.call(REQUEST_IDENTITY_VERIFICATION, {}, self.ctx)
        self.assertEqual(envelope["error"]["code"], "no_number_on_file")

    def test_the_tool_is_absent_when_nothing_can_look_codes_up(self):
        registry = build_registry(verification=VerificationStore())
        self.assertNotIn(FIND_ACCOUNT_BY_CODE, registry.specs)


class IntakeTicketTests(unittest.TestCase):
    """The failure path: identity never proved, but the case still lands somewhere."""

    def setUp(self):
        self.registry = build_registry(verification=VerificationStore())
        self.ctx = ToolContext(conversation_id="c1")

    def _raise(self, **kw):
        args = {"summary": "Motor dead, customer says in warranty", "idempotency_key": "k1"}
        args.update(kw)
        return self.registry.call(RAISE_INTAKE_TICKET, args, self.ctx)

    def test_it_works_without_a_resolved_phone(self):
        # create_support_ticket refuses here, which left an unverified customer
        # with no path at all — and an agent that promises a ticket it cannot
        # raise is worse than one that says no.
        verified_path = self.registry.call(
            "create_support_ticket",
            {"category": "other", "description": "x", "severity": "normal", "idempotency_key": "k0"},
            self.ctx,
        )
        self.assertEqual(verified_path["error"]["code"], "missing_identity")
        self.assertFalse(is_error(self._raise()))

    def test_everything_is_recorded_as_claimed_not_confirmed(self):
        data = self._raise(stated_name="Radhika", stated_contact="r@example.com")["data"]
        self.assertEqual(data["identity"], "unverified")
        ticket = self.registry.tickets.tickets[data["ticket_id"]]
        self.assertEqual(ticket["identity"], "unverified")
        self.assertEqual(ticket["stated_name"], "Radhika")
        self.assertEqual(ticket["category"], "intake_unverified")

    def test_absent_details_are_recorded_as_absent_rather_than_blank(self):
        ticket = self.registry.tickets.tickets[self._raise()["data"]["ticket_id"]]
        self.assertEqual(ticket["stated_name"], "not given")
        self.assertEqual(ticket["evidence"], "none offered")

    def test_it_never_asserts_a_warranty_outcome(self):
        data = self._raise()["data"]
        self.assertNotIn("coverage", str(data).lower())
        self.assertIn("verify", data["expected_response"])


if __name__ == "__main__":
    unittest.main()
