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

    def test_going_back_comes_after_safety_and_before_the_handoff(self):
        self.assertEqual(NODE_NAMES[1:4], ("safety_gate", "navigation_gate", "handoff_gate"))


if __name__ == "__main__":
    unittest.main()
