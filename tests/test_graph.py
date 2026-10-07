import unittest

from emotorad_ai.graph import NODE_NAMES
from emotorad_ai.llm import say
from tests.test_agent_and_runtime import make_runtime, send


class GraphShapeTests(unittest.TestCase):
    def test_the_runtime_turn_is_a_graph_with_every_step_as_a_node(self):
        runtime, _, _ = make_runtime([])
        nodes = set(runtime.graph.get_graph().nodes)
        self.assertTrue(set(NODE_NAMES) <= nodes, nodes)

    def test_a_turn_through_the_graph_returns_the_reply(self):
        runtime, adapter, llm = make_runtime([say("Check the socket first.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("Check the socket first.", reply.text)
        self.assertEqual(reply.handled_by, "battery_support")

    def test_the_callback_number_then_going_back_come_after_safety_and_before_the_handoff(self):
        self.assertEqual(NODE_NAMES[1:5], ("safety_gate", "callback_gate", "navigation_gate", "handoff_gate"))

    def test_the_serial_confirmation_then_the_melt_ask_come_after_persona_routing_and_before_jev(self):
        # The serial confirmation (7 October 2026) and the melt ask (6 October
        # 2026): after triage has chosen the bike and routed, before Jev or any
        # agent, so no model runs on a turn either of them answers.
        self.assertEqual(NODE_NAMES[NODE_NAMES.index("persona_route") + 1], "serial_confirm")
        self.assertEqual(NODE_NAMES[NODE_NAMES.index("serial_confirm") + 1], "melt_ask")
        self.assertEqual(NODE_NAMES[NODE_NAMES.index("melt_ask") + 1], "jev_classify")
        runtime, _, _ = make_runtime([])
        edges = {(edge.source, edge.target) for edge in runtime.graph.get_graph().edges}
        self.assertIn(("persona_route", "serial_confirm"), edges)
        self.assertIn(("serial_confirm", "melt_ask"), edges)
        self.assertIn(("serial_confirm", "__end__"), edges)
        self.assertIn(("melt_ask", "jev_classify"), edges)
        self.assertIn(("melt_ask", "__end__"), edges)
        self.assertNotIn(("persona_route", "jev_classify"), edges)
        self.assertNotIn(("persona_route", "melt_ask"), edges)

if __name__ == "__main__":
    unittest.main()
