"""The chat keeps the customer's area, never coordinates (spec 2026-10-09, section 3)."""

import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.config import Settings
from emotorad_ai.conversation import ConversationState
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TURN_FACT_FIELDS, Runtime
from emotorad_ai.tools.mocks import build_registry

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def runtime_with(responses):
    registry = build_registry(today=date(2026, 10, 9))
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry,
                      llm=ScriptedClaude(responses), log=EventLog(path=None), resolver=IdentityResolver(registry),
                      self_service_identity=True)
    return runtime, WebsiteChatAdapter(runtime.resolver)


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-a").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-a", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class AreaTests(unittest.TestCase):
    def test_the_area_is_a_turn_fact(self):
        self.assertIn("area", TURN_FACT_FIELDS)
        self.assertIsNone(ConversationState(conversation_id="x").area)

    def test_a_messages_area_is_kept_and_survives_the_next_message(self):
        runtime, adapter = runtime_with([say("Thanks."), say("Sure.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertEqual(runtime.conversations.get("conv-a").area, AREA)
        send(runtime, adapter, "and another thing")
        self.assertEqual(runtime.conversations.get("conv-a").area, AREA)

    def test_a_later_area_replaces_it(self):
        runtime, adapter = runtime_with([say("Thanks."), say("Sure.")])
        send(runtime, adapter, "hi", area=AREA)
        later = dict(AREA, pincode="400054", district="Mumbai")
        send(runtime, adapter, "I'm in Mumbai now", area=later)
        self.assertEqual(runtime.conversations.get("conv-a").area["pincode"], "400054")


if __name__ == "__main__":
    unittest.main()
