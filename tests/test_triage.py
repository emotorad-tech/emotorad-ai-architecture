import unittest

from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import (
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    ROUTED,
    ConversationState,
)
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.triage import TriageAgent, classify_issue, match_bike

BIKES = [
    {"frame_number": "TREX2024881201", "product_name": "T-Rex Air", "product_color": "Blue"},
    {"frame_number": "EMXP2025004990", "product_name": "EMX Plus", "product_color": "Grey"},
    {"frame_number": "DDL32021100455", "product_name": "Doodle V3", "product_color": "Red"},
]

TOPIC_AGENTS = {"battery": "battery_support", "motor": "motor_support"}


def resolved(bikes=BIKES):
    return ResolvedIdentity(
        persona="customer",
        method="verified",
        identity=Identity(cluster_id="clu-1", strength=VERIFIED, phone="+919700000001"),
        profile={"name": "Priya Nair"},
        bikes=list(bikes),
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


class ClassificationTests(unittest.TestCase):
    def test_obvious_battery_and_motor_complaints_are_classified(self):
        self.assertEqual(classify_issue("my battery won't charge"), "battery")
        self.assertEqual(classify_issue("the motor makes a grinding noise"), "motor")

    def test_hindi_and_hinglish_are_not_silent_misses(self):
        self.assertEqual(classify_issue("बैटरी चार्ज नहीं हो रही"), "battery")
        self.assertEqual(classify_issue("motor se awaz aa rahi hai"), "motor")

    def test_an_unclear_message_returns_none_rather_than_guessing(self):
        # Forcing a guess is how a motor complaint lands in a battery agent and
        # gets confidently troubleshooted for the wrong component.
        self.assertIsNone(classify_issue("hi"))
        self.assertIsNone(classify_issue("I need some help please"))

    def test_a_message_about_both_is_ambiguous_not_a_coin_flip(self):
        self.assertIsNone(classify_issue("the motor is noisy and the battery drains fast"))


class BikeMatchingTests(unittest.TestCase):
    def test_ordinal_selection(self):
        self.assertEqual(match_bike("1", BIKES)["frame_number"], "TREX2024881201")
        self.assertEqual(match_bike("the second one", BIKES)["frame_number"], "EMXP2025004990")
        self.assertEqual(match_bike("तीसरी", BIKES)["frame_number"], "DDL32021100455")

    def test_model_name_selection(self):
        self.assertEqual(match_bike("the EMX Plus", BIKES)["frame_number"], "EMXP2025004990")
        self.assertEqual(match_bike("doodle", BIKES)["frame_number"], "DDL32021100455")

    def test_frame_number_and_its_tail(self):
        self.assertEqual(match_bike("EMXP2025004990", BIKES)["frame_number"], "EMXP2025004990")
        self.assertEqual(match_bike("4990", BIKES)["frame_number"], "EMXP2025004990")

    def test_two_bikes_of_the_same_model_need_a_colour(self):
        twins = [
            {"frame_number": "EMXP0001", "product_name": "EMX Plus", "product_color": "Grey"},
            {"frame_number": "EMXP0002", "product_name": "EMX Plus", "product_color": "Blue"},
        ]
        self.assertIsNone(match_bike("the EMX Plus", twins), "ambiguous must not resolve")
        self.assertEqual(match_bike("the blue EMX Plus", twins)["frame_number"], "EMXP0002")

    def test_an_unrelated_reply_matches_nothing(self):
        self.assertIsNone(match_bike("actually can I speak to someone", BIKES))
        self.assertIsNone(match_bike("", BIKES))

    def test_an_ordinal_beyond_the_list_is_not_a_match(self):
        self.assertIsNone(match_bike("3", BIKES[:2]))


class TriageFlowTests(unittest.TestCase):
    def setUp(self):
        self.triage = TriageAgent(TOPIC_AGENTS)

    def test_a_single_bike_owner_skips_selection_entirely(self):
        state = ConversationState("c1")
        outcome = self.triage.handle(message("battery won't charge"), resolved(BIKES[:1]), state)
        self.assertTrue(outcome.is_handoff)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "TREX2024881201")
        self.assertEqual(state.phase, ROUTED)

    def test_three_bikes_forces_a_choice_before_any_routing(self):
        state = ConversationState("c1")
        outcome = self.triage.handle(message("battery won't charge"), resolved(), state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("Which one", outcome.reply)
        self.assertEqual(state.phase, AWAITING_BIKE_SELECTION)

    def test_the_topic_survives_the_bike_selection_turn(self):
        # The customer said what was wrong before we asked which bike. Asking
        # them to repeat it would be maddening.
        state = ConversationState("c1")
        who = resolved()
        self.triage.handle(message("battery won't charge"), who, state)
        outcome = self.triage.handle(message("the EMX Plus"), who, state)

        self.assertTrue(outcome.is_handoff)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(state.selected_frame, "EMXP2025004990")

    def test_a_tapped_pill_still_has_to_pass_through_bike_selection(self):
        state = ConversationState("c1")
        outcome = self.triage.handle(message("", pill="battery"), resolved(), state)
        self.assertFalse(outcome.is_handoff, "a pill says the topic, not the bike")
        self.assertEqual(state.pending_topic, "battery")

    def test_an_unmatched_selection_re_asks_instead_of_picking_one(self):
        state = ConversationState("c1")
        who = resolved()
        self.triage.handle(message("battery issue"), who, state)
        outcome = self.triage.handle(message("what are your opening hours?"), who, state)

        self.assertFalse(outcome.is_handoff)
        self.assertIn("did not catch", outcome.reply)
        self.assertIsNone(state.selected_frame)
        self.assertEqual(state.phase, AWAITING_BIKE_SELECTION)

    def test_a_vague_opener_asks_what_is_wrong(self):
        state = ConversationState("c1")
        outcome = self.triage.handle(message("hi"), resolved(BIKES[:1]), state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("What is happening", outcome.reply)
        self.assertEqual(state.phase, AWAITING_ISSUE)
        self.assertEqual(state.selected_frame, "TREX2024881201", "bike is known even if issue isn't")

    def test_a_topic_with_no_agent_says_so_rather_than_defaulting(self):
        triage = TriageAgent({"battery": "battery_support"})  # no motor agent
        state = ConversationState("c1")
        outcome = triage.handle(message("the motor is noisy"), resolved(BIKES[:1]), state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("support team", outcome.reply)
        self.assertEqual(state.phase, AWAITING_ISSUE)

    def test_a_customer_with_no_bikes_is_still_triaged(self):
        state = ConversationState("c1")
        outcome = self.triage.handle(message("battery won't charge"), resolved([]), state)
        self.assertTrue(outcome.is_handoff)
        self.assertIsNone(state.selected_frame)

    def test_switching_bike_after_routing_clears_the_agent(self):
        state = ConversationState("c1")
        who = resolved()
        self.triage.handle(message("battery issue"), who, state)
        self.triage.handle(message("1"), who, state)
        self.assertEqual(state.agent, "battery_support")

        state.move_to(AWAITING_BIKE_SELECTION, "customer changed bike")
        self.triage.handle(message("the doodle"), who, state)

        self.assertEqual(state.selected_frame, "DDL32021100455")
        self.assertIn("bike_changed:TREX2024881201->DDL32021100455", state.transitions)


if __name__ == "__main__":
    unittest.main()
