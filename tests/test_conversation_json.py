import json
import unittest

from emotorad_ai.conversation import ConversationState

HISTORY = [
    {"role": "user", "content": "बैटरी चार्ज नहीं हो रही"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {"query": "won't charge"}},
    ]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"data\": {}}", "is_error": False}]},
]


class StateJsonTests(unittest.TestCase):
    def test_every_field_round_trips_including_history(self):
        state = ConversationState(conversation_id="c1", selected_frame="EMXP2025004417", agent="battery_support",
                                  sub_category="battery-wont-charge", turns=2, disclosed=True, evidence_seen=True,
                                  history=list(HISTORY), transitions=["greeting->routed"], version=3,
                                  user_key="PHONE#+919876543210", started_at="2026-09-28T10:00:00+00:00",
                                  channel="whatsapp", escalated=True, ticket_id="EM-00001")
        again = ConversationState.from_json(state.to_json())
        self.assertEqual(again, state)
        self.assertEqual(json.loads(state.to_json())["history"][0]["content"], "बैटरी चार्ज नहीं हो रही")

    def test_unknown_fields_from_a_newer_version_are_ignored(self):
        raw = json.dumps({"conversation_id": "c1", "a_field_from_the_future": 1})
        self.assertEqual(ConversationState.from_json(raw).conversation_id, "c1")


if __name__ == "__main__":
    unittest.main()
