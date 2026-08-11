import unittest

from emotorad_ai.conversation import (
    AWAITING_BIKE_SELECTION,
    AWAITING_ISSUE,
    GREETING,
    ROUTED,
    ConversationState,
    ConversationStore,
)


class StateTests(unittest.TestCase):
    def test_a_new_conversation_starts_at_greeting_and_undisclosed(self):
        state = ConversationState("c1")
        self.assertEqual(state.phase, GREETING)
        self.assertFalse(state.disclosed)
        self.assertIsNone(state.selected_frame)

    def test_unknown_phases_are_rejected_at_construction_and_transition(self):
        with self.assertRaises(ValueError):
            ConversationState("c1", phase="vibing")
        with self.assertRaises(ValueError):
            ConversationState("c1").move_to("vibing")

    def test_transitions_are_recorded_for_debugging(self):
        state = ConversationState("c1")
        state.move_to(AWAITING_BIKE_SELECTION, "3 bikes")
        state.move_to(AWAITING_ISSUE)
        self.assertEqual(
            state.transitions,
            ["greeting->awaiting_bike_selection:3 bikes", "awaiting_bike_selection->awaiting_issue"],
        )

    def test_routing_records_the_owning_agent(self):
        state = ConversationState("c1")
        state.route_to("battery_support")
        self.assertEqual(state.phase, ROUTED)
        self.assertEqual(state.agent, "battery_support")

    def test_changing_bike_mid_conversation_resets_the_routed_agent(self):
        # Troubleshooting already done applies to a different bike. Keeping the
        # agent would continue a battery diagnosis against the wrong machine.
        state = ConversationState("c1")
        state.select_bike("FRAME-A")
        state.route_to("battery_support")

        state.select_bike("FRAME-B")

        self.assertIsNone(state.agent)
        self.assertEqual(state.selected_frame, "FRAME-B")
        self.assertIn("bike_changed:FRAME-A->FRAME-B", state.transitions)

    def test_reselecting_the_same_bike_is_not_a_change(self):
        state = ConversationState("c1")
        state.select_bike("FRAME-A")
        state.route_to("battery_support")
        state.select_bike("FRAME-A")
        self.assertEqual(state.agent, "battery_support")

    def test_handing_back_keeps_the_bike_but_clears_the_agent(self):
        # "Battery's fine now, but the motor is noisy" is a new issue on the same
        # bike. Asking which bike again would be maddening.
        state = ConversationState("c1")
        state.select_bike("FRAME-A")
        state.route_to("battery_support")

        state.hand_back("issue_resolved")

        self.assertIsNone(state.agent)
        self.assertEqual(state.selected_frame, "FRAME-A")
        self.assertEqual(state.phase, AWAITING_ISSUE)


class StoreTests(unittest.TestCase):
    def test_the_same_conversation_id_returns_the_same_state(self):
        store = ConversationStore()
        first = store.get("c1")
        first.select_bike("FRAME-A")
        self.assertEqual(store.get("c1").selected_frame, "FRAME-A")
        self.assertEqual(len(store), 1)

    def test_different_conversations_do_not_share_state(self):
        store = ConversationStore()
        store.get("c1").select_bike("FRAME-A")
        self.assertIsNone(store.get("c2").selected_frame)
        self.assertEqual(len(store), 2)


if __name__ == "__main__":
    unittest.main()
