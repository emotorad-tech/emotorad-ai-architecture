"""Triage classifies against a table it is *given*, so a bot added as
configuration is routable without editing triage.py. The defaults are the
built-in agents' own tables, so nothing about them changes."""

import unittest

from emotorad_ai.agents import battery_support, motor_support
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.triage import TOPIC_KEYWORDS, TriageAgent, classify_issue, topic_from_pill

BIKE = {"frame_number": "EMXP2025004990", "product_name": "EMX Plus", "product_color": "Grey"}
BRAKES = {"battery": ("battery",), "brakes": ("brake", "braking", "ब्रेक")}


def resolved():
    return ResolvedIdentity(
        persona="customer",
        method="verified",
        identity=Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000001"),
        profile={"name": "Priya Nair"},
        bikes=[BIKE],
    )


def message(text, pill=None):
    return InboundMessage(
        "c1",
        "customer",
        Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000001"),
        "whatsapp",
        text,
        entry_metadata={"pill_clicked": pill} if pill else {},
    )


class KeywordTableTests(unittest.TestCase):
    def test_the_default_table_is_the_agent_modules_own(self):
        self.assertIs(TOPIC_KEYWORDS["battery"], battery_support.KEYWORDS)
        self.assertIs(TOPIC_KEYWORDS["motor"], motor_support.KEYWORDS)
        self.assertEqual(battery_support.TOPIC, "battery")
        self.assertEqual(motor_support.TOPIC, "motor")

    def test_classify_issue_uses_the_table_it_is_given(self):
        self.assertEqual(classify_issue("my brake is squeaking", BRAKES), "brakes")
        self.assertEqual(classify_issue("ब्रेक काम नहीं कर रहा", BRAKES), "brakes")
        self.assertIsNone(classify_issue("motor is noisy", BRAKES), "motor is not in this table")

    def test_topic_from_pill_uses_the_table_it_is_given(self):
        self.assertEqual(topic_from_pill("brakes", BRAKES), "brakes")
        self.assertEqual(topic_from_pill("brake_issue", BRAKES), "brakes")
        self.assertIsNone(topic_from_pill("motor_issue", BRAKES))


class TriageAgentTableTests(unittest.TestCase):
    def test_a_custom_topic_routes_to_its_agent(self):
        triage = TriageAgent({"brakes": "brakes_support"}, BRAKES, "brake problems")
        state = ConversationState("c1")
        outcome = triage.handle(message("the brake is squeaking"), resolved(), state)
        self.assertTrue(outcome.is_handoff)
        self.assertEqual(outcome.agent, "brakes_support")
        self.assertEqual(outcome.reason, "text->brakes")

    def test_the_unsupported_reply_names_what_is_supported(self):
        triage = TriageAgent({"brakes": "brakes_support"}, BRAKES, "brake problems")
        state = ConversationState("c1")
        outcome = triage.handle(message("battery won't charge"), resolved(), state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("I can help with brake problems from here.", outcome.reply)

    def test_the_default_summary_is_unchanged(self):
        triage = TriageAgent({"battery": "battery_support"})
        state = ConversationState("c1")
        outcome = triage.handle(message("the motor is noisy"), resolved(), state)
        self.assertIn("I can help with battery and motor problems from here.", outcome.reply)


if __name__ == "__main__":
    unittest.main()
