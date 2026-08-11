import unittest

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.contract import (
    ANONYMOUS,
    ASSERTED,
    VERIFIED,
    Identity,
    InboundMessage,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.tools.mocks import build_registry


def make_adapter():
    return WebsiteChatAdapter(IdentityResolver(build_registry()))


class ContractTests(unittest.TestCase):
    def test_rejects_unknown_persona_and_channel(self):
        with self.assertRaises(ValueError):
            InboundMessage("c1", "supplier", Identity(), "website_chat", "hi")
        with self.assertRaises(ValueError):
            InboundMessage("c1", "customer", Identity(), "carrier_pigeon", "hi")

    def test_serialises_to_the_documented_shape(self):
        message = InboundMessage(
            conversation_id="c1",
            persona="customer",
            identity=Identity(
                cluster_id="clu-1", strength=VERIFIED, phone="+919876543210",
                channel_user_id="sess-ananya",
            ),
            channel="website_chat",
            message_text="battery won't charge",
            entry_metadata={"pill_clicked": "battery_issue"},
        )
        payload = message.to_dict()
        self.assertEqual(payload["persona"], "customer")
        self.assertEqual(payload["identity"]["cluster_id"], "clu-1")
        self.assertEqual(payload["identity"]["strength"], VERIFIED)
        self.assertEqual(payload["message"]["text"], "battery won't charge")
        self.assertEqual(message.pill_clicked, "battery_issue")

    def test_strength_gates_disclosure_in_code(self):
        self.assertTrue(Identity(cluster_id="c", strength=VERIFIED).may_disclose)
        # Caller ID is spoofable, so it resolves a person but authorises nothing.
        self.assertFalse(Identity(cluster_id="c", strength=ASSERTED).may_disclose)
        self.assertFalse(Identity(cluster_id="c", strength=ANONYMOUS).may_disclose)
        with self.assertRaises(ValueError):
            Identity(strength="probably")

    def test_subject_is_rejected_for_personas_that_speak_for_themselves(self):
        employee = Identity(cluster_id="e1", strength=VERIFIED, employee_email="ops@emotorad.com")
        customer = Identity(cluster_id="c1", strength=VERIFIED, phone="+919876543210")

        # An employee asking about a customer: actor and subject both travel.
        message = InboundMessage(
            "c1", "internal", employee, "internal_portal", "what's her coverage?", subject=customer
        )
        self.assertEqual(message.about.phone, "+919876543210")
        self.assertEqual(message.identity.employee_email, "ops@emotorad.com")

        # A customer asking about someone else is not a thing the contract allows.
        with self.assertRaises(ValueError):
            InboundMessage("c2", "customer", customer, "website_chat", "hi", subject=customer)

    def test_about_collapses_to_the_sender_when_there_is_no_subject(self):
        me = Identity(cluster_id="c1", strength=VERIFIED, phone="+919876543210")
        message = InboundMessage("c1", "customer", me, "website_chat", "hi")
        self.assertIs(message.about, me)


class WebsiteAdapterTests(unittest.TestCase):
    def test_logged_in_session_resolves_to_a_verified_customer(self):
        message = make_adapter().to_message(
            {
                "em_aid": "cookie-laptop",
                "session_token": "sess-ananya",
                "text": "  hello  ",
                "pill": "battery_issue",
            }
        )
        self.assertEqual(message.persona, "customer")
        self.assertEqual(message.identity.strength, VERIFIED)
        self.assertEqual(message.identity.phone, "+919876543210")
        self.assertTrue(message.identity.cluster_id)
        self.assertEqual(message.message_text, "hello")
        self.assertEqual(message.pill_clicked, "battery_issue")

    def test_anonymous_visitor_is_a_valid_identity_not_a_failure(self):
        # A cookie with no session is the common case, and it must still get a
        # cluster and a conversation — just no personal facts.
        message = make_adapter().to_message({"em_aid": "cookie-new", "text": "hello"})
        self.assertEqual(message.persona, "customer")
        self.assertEqual(message.identity.strength, ANONYMOUS)
        self.assertTrue(message.identity.cluster_id)
        self.assertFalse(message.identity.may_disclose)

    def test_a_visitor_with_neither_cookie_nor_session_is_unknown(self):
        message = make_adapter().to_message({"text": "hello"})
        self.assertEqual(message.persona, "unknown")
        self.assertIsNone(message.identity.cluster_id)

    def test_the_same_cookie_returns_to_the_same_cluster(self):
        adapter = make_adapter()
        first = adapter.to_message({"em_aid": "cookie-x", "text": "hi"})
        second = adapter.to_message({"em_aid": "cookie-x", "text": "again"})
        self.assertEqual(first.identity.cluster_id, second.identity.cluster_id)


if __name__ == "__main__":
    unittest.main()
