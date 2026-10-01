import unittest

from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import (
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    AWAITING_UNLISTED_BIKE,
    ROUTED,
    ConversationState,
)
from emotorad_ai.identity import ResolvedIdentity
from emotorad_ai.triage import (
    ASK_FOR_MODEL,
    ASK_FOR_UNLISTED_BIKE,
    TriageAgent,
    classify_issue,
    describe_bike,
    match_bike,
    says_no,
    says_yes,
    which_bike_text,
)

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


class FullFrameNumbersTests(unittest.TestCase):
    """The person's rule (2026-09-30): the list shows each bike's frame number
    so the customer can match it to the sticker on the frame."""

    def test_the_whole_frame_number_is_shown(self):
        self.assertEqual(
            describe_bike({"product_name": "EMX Plus", "product_color": "Grey", "frame_number": "EMXP2026001234"}),
            "EMX Plus (Grey), frame EMXP2026001234",
        )
        self.assertEqual(describe_bike({"product_name": "EMX Plus", "product_color": "", "frame_number": "F1"}),
                         "EMX Plus, frame F1")

    def test_several_bikes_ask_which(self):
        text = which_bike_text(BIKES)
        self.assertIn("I found %d bikes on this number:" % len(BIKES), text)
        for bike in BIKES:
            self.assertIn(bike["frame_number"], text)
        self.assertIn("Which one needs help?", text)

    def test_one_bike_asks_to_confirm(self):
        text = which_bike_text(BIKES[:1])
        self.assertIn("I found 1 bike on this number:", text)
        self.assertIn("Is this the bike that needs help? Reply yes", text)


class YesNoTests(unittest.TestCase):
    def test_yes(self):
        for typed in ("yes", "Yes, that's the one", "haan", "ji", "correct", "ok", "हाँ"):
            self.assertTrue(says_yes(typed), typed)
        for typed in ("yesterday it stopped", "no", "okayish"):
            self.assertFalse(says_yes(typed), typed)

    def test_no(self):
        for typed in ("no", "No.", "nope", "nahi", "नहीं", "not this one", "a different one"):
            self.assertTrue(says_no(typed), typed)
        for typed in ("no power at all", "yes", "nothing happens"):
            self.assertFalse(says_no(typed), typed)

    def test_a_polite_no_is_a_no_and_not_a_yes(self):
        # Finding 5 of the final review: "ji nahi" began with "ji" and was a yes.
        for typed in ("ji nahi", "जी नहीं", "no, a different one", "nahi, dusri"):
            self.assertTrue(says_no(typed), typed)
            self.assertFalse(says_yes(typed), typed)
        self.assertFalse(says_yes("ok but it's the other one"))
        self.assertTrue(says_yes("yes it's not charging"))


class OneBikeSelectionTests(unittest.TestCase):
    """Only the verify-first step puts one bike into selection; triage on its
    own still picks a lone bike without asking."""

    def setUp(self):
        self.triage = TriageAgent(TOPIC_AGENTS)
        self.state = ConversationState("c1")
        self.state.move_to(AWAITING_BIKE_SELECTION, "verified")
        self.state.pending_topic = "battery"

    def test_yes_selects_the_bike_and_routes_by_the_kept_topic(self):
        outcome = self.triage.handle(message("yes"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertEqual(self.state.selected_frame, BIKES[0]["frame_number"])

    def test_no_asks_for_the_frame_number_and_model(self):
        # The bike not listed (spec 2026-10-01, unlisted bike): no route to
        # late registration any more.
        outcome = self.triage.handle(message("no"), resolved(BIKES[:1]), self.state)
        self.assertFalse(outcome.is_handoff)
        self.assertEqual(outcome.reply, ASK_FOR_UNLISTED_BIKE)
        self.assertEqual(self.state.phase, AWAITING_UNLISTED_BIKE)
        self.assertIsNone(self.state.selected_frame)

    def test_ji_nahi_asks_for_the_frame_number_and_model(self):
        outcome = self.triage.handle(message("ji nahi"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.reply, ASK_FOR_UNLISTED_BIKE)
        self.assertIsNone(self.state.selected_frame)

    def test_the_frame_number_of_a_bike_not_listed_asks_for_its_model(self):
        # The question invites it ("send the frame number of the bike you
        # mean"); it used to loop on "did not catch" for ever.
        outcome = self.triage.handle(message("DDL32023045678"), resolved(BIKES[:1]), self.state)
        self.assertEqual(outcome.reply, ASK_FOR_MODEL)
        self.assertEqual(self.state.unlisted_bike["frame_number"], "DDL32023045678")

    def test_anything_else_asks_again(self):
        outcome = self.triage.handle(message("what?"), resolved(BIKES[:1]), self.state)
        self.assertFalse(outcome.is_handoff)
        self.assertIn("did not catch", outcome.reply)
        self.assertIn(BIKES[0]["frame_number"], outcome.reply)

    def test_no_bikes_to_choose_from_carries_on_to_the_issue(self):
        # A lookup that failed after verification: no list to ask about.
        outcome = self.triage.handle(message("2"), resolved([]), self.state)
        self.assertEqual(outcome.agent, "battery_support")
        self.assertNotIn("0 bikes", outcome.reply or "")


if __name__ == "__main__":
    unittest.main()
