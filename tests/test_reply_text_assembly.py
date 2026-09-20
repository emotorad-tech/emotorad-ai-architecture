"""Everything the model wrote during a turn has to reach the customer.

Reported from a phone on 20 September, conversation 53c93cd3. The customer
typed their one-time code and got back, in full:

    "The picture should be just above this message."

with a photo of a battery on/off switch and nothing else. No name, no bike, no
reason for the picture. The log shows the turn did four things — verified the
code, looked up the bike, searched the knowledge base and sent the guide photo —
and the customer was told about none of them.

The model had written the explanation in the same block as the `send_guide_media`
call, which is normal and correct behaviour: a preamble saying what it is about
to do. `Agent.run` assigned `turn.text` from the final tool-free response only,
so every earlier block of prose was appended to the history the model sees and
never sent to the person it was written for. The customer was left reading the
last sentence of a paragraph they never received.
"""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry


def _reply(responses):
    registry = build_registry()
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=ScriptedClaude(responses),
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    runtime.conversations.get("c1").route_to("battery_support")
    return runtime.handle(
        InboundMessage(
            conversation_id="c1",
            persona="customer",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
            channel="website_chat",
            message_text="my bike will not start",
        )
    )


class PreambleReachesTheCustomerTests(unittest.TestCase):
    def test_text_written_beside_a_tool_call_is_not_lost(self):
        """The exact shape of conversation 53c93cd3."""
        reply = _reply([
            call_tool(
                "search_knowledge",
                {"query": "will not power on"},
                text="Thanks. Before anything else, check the battery's own on/off switch is ON.",
            ),
            say("The picture should be just above this message."),
        ])
        self.assertIn("on/off switch is ON", reply.text)

    def test_the_closing_line_is_kept_too(self):
        reply = _reply([
            call_tool("search_knowledge", {"query": "x"}, text="First, check the switch."),
            say("The picture should be just above this message."),
        ])
        self.assertIn("First, check the switch.", reply.text)
        self.assertIn("just above this message", reply.text)

    def test_it_keeps_the_order_it_was_written_in(self):
        reply = _reply([
            call_tool("search_knowledge", {"query": "x"}, text="First."),
            call_tool("find_service_slots", {"pincode": "411001"}, text="Second."),
            say("Third."),
        ])
        self.assertLess(reply.text.index("First."), reply.text.index("Second."))
        self.assertLess(reply.text.index("Second."), reply.text.index("Third."))

    def test_a_turn_with_no_preamble_is_unchanged(self):
        """The ordinary case must not gain blank lines or stray whitespace."""
        reply = _reply([
            call_tool("search_knowledge", {"query": "x"}),
            say("Here is what to do."),
        ])
        self.assertIn("Here is what to do.", reply.text)
        self.assertNotIn("\n\n\n", reply.text)


class EmptyFinalBlockTests(unittest.TestCase):
    def test_a_preamble_alone_is_still_a_reply(self):
        """The model explained itself, called its tool, and ended the turn
        without a closing line. It said something useful; handing the customer
        to a human instead would throw that away."""
        reply = _reply([
            call_tool("search_knowledge", {"query": "x"}, text="Check the on/off switch is ON."),
            say(""),
        ])
        self.assertIn("Check the on/off switch is ON.", reply.text)
        self.assertFalse(reply.escalated)

    def test_a_turn_that_says_nothing_at_all_still_hands_over(self):
        """Nothing written anywhere is still a failed turn."""
        reply = _reply([
            call_tool("search_knowledge", {"query": "x"}),
            say(""),
        ])
        self.assertTrue(reply.escalated)


if __name__ == "__main__":
    unittest.main()
