import unittest
from datetime import date

from emotorad_ai.agents.late_warranty import AGENT_NAME, DEFINITION
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver, ResolvedIdentity
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import SUBMIT_WARRANTY_PROOF, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error

TODAY = date(2026, 8, 3)


def message(channel="whatsapp", text="I never registered my bike"):
    return InboundMessage(
        "c1",
        "customer",
        Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000009"),
        channel,
        text,
    )


class OpeningTests(unittest.TestCase):
    """The two entry paths must not sound the same to the customer."""

    def setUp(self):
        self.resolver = IdentityResolver(build_registry(today=TODAY))

    def _resolved(self, phone):
        _, identity = self.resolver.resolve_whatsapp(phone)
        return self.resolver.hydrate(
            InboundMessage("c1", "customer", identity, "whatsapp", "hi")
        )

    def test_an_unregistered_bike_is_asked_to_register(self):
        prompt = DEFINITION.build_system_prompt(
            message(), self._resolved(fixtures.PHONE_WITH_NO_RECORD.lstrip("+"))
        )
        self.assertIn("no bike is registered", prompt)
        self.assertIn("frame number", prompt.lower())

    def test_a_registered_bike_with_no_date_is_never_told_to_register(self):
        # Telling someone whose bike we can see that they need to register it
        # reads as though we lost their record.
        prompt = DEFINITION.build_system_prompt(message(), self._resolved("919700000002"))
        self.assertIn("IS registered", prompt)
        self.assertIn("Do NOT ask them to register", prompt)
        self.assertIn("EMXP2024773311", prompt, "acknowledge the bike by name")

    def test_both_paths_forbid_stating_coverage(self):
        for phone in (fixtures.PHONE_WITH_NO_RECORD.lstrip("+"), "919700000002"):
            prompt = DEFINITION.build_system_prompt(message(), self._resolved(phone))
            self.assertIn("Never state, estimate or confirm any warranty coverage", prompt)


class ChannelCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.resolved = ResolvedIdentity(
            persona="customer",
            method="no_warranty_record",
            identity=Identity(cluster_id="c", strength=VERIFIED, phone="+919700000009"),
        )

    def test_upload_capable_channels_may_ask_for_the_invoice(self):
        for channel in ("whatsapp", "website_chat", "amiigo_app"):
            prompt = DEFINITION.build_system_prompt(message(channel), self.resolved)
            self.assertIn("can send a photo or file", prompt, channel)

    def test_voice_never_promises_an_upload_it_cannot_accept(self):
        # There is nowhere to put an invoice on an IVR call. Asking strands them.
        prompt = DEFINITION.build_system_prompt(message("voice"), self.resolved)
        self.assertIn("cannot accept a file", prompt)
        self.assertIn("WhatsApp", prompt)


class ProofSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)
        self.ctx = ToolContext(conversation_id="c1", phone="+919700000009")

    def test_submitting_proof_never_sets_coverage(self):
        # The whole risk of this flow: a customer-supplied date deciding what we
        # owe them, with no human having read the document.
        envelope = self.registry.call(
            SUBMIT_WARRANTY_PROOF,
            {
                "frame_number": "EMXP9999999999",
                "claimed_purchase_date": "2020-01-01",
                "proof_url": "https://example.test/invoice.jpg",
                "purchase_channel": "marketplace",
                "idempotency_key": "proof-1",
            },
            self.ctx,
        )
        data = envelope["data"]
        self.assertFalse(data["coverage_set"])
        self.assertEqual(data["status"], "awaiting_human_verification")
        self.assertIn("Do not tell the customer their warranty is now active", data["note"])

    def test_the_claimed_date_is_recorded_as_a_claim_not_a_fact(self):
        self.registry.call(
            SUBMIT_WARRANTY_PROOF,
            {
                "frame_number": "EMXP9999999999",
                "claimed_purchase_date": "2020-01-01",
                "idempotency_key": "proof-2",
            },
            self.ctx,
        )
        submission = list(self.registry.tickets.tickets.values())[-1]
        self.assertFalse(submission["verified"])
        self.assertEqual(submission["claimed_purchase_date"], "2020-01-01")
        self.assertIn("REQUIRES HUMAN VERIFICATION", submission["description"])

    def test_submission_is_idempotent(self):
        args = {"frame_number": "EMXP9999999999", "idempotency_key": "proof-3"}
        first = self.registry.call(SUBMIT_WARRANTY_PROOF, dict(args), self.ctx)
        second = self.registry.call(SUBMIT_WARRANTY_PROOF, dict(args), self.ctx)
        self.assertEqual(first["data"]["reference"], second["data"]["reference"])
        self.assertEqual(len(self.registry.tickets.tickets), 1)

    def test_a_write_without_an_idempotency_key_is_refused(self):
        envelope = self.registry.call(
            SUBMIT_WARRANTY_PROOF, {"frame_number": "EMXP9999999999"}, self.ctx
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "missing_idempotency_key")

    def test_an_unknown_purchase_channel_is_rejected(self):
        envelope = self.registry.call(
            SUBMIT_WARRANTY_PROOF,
            {
                "frame_number": "EMXP9999999999",
                "purchase_channel": "found_it_in_a_field",
                "idempotency_key": "proof-4",
            },
            self.ctx,
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "invalid_channel")

    def test_the_agent_only_gets_the_one_tool_it_needs(self):
        self.assertEqual(tuple(DEFINITION.tool_names), (SUBMIT_WARRANTY_PROOF,))
        self.assertEqual(DEFINITION.name, AGENT_NAME)


if __name__ == "__main__":
    unittest.main()
