"""The nearest dealers through runtime.handle() (spec 2026-10-09, sections 4 to 6)."""

import json
import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import DEALER_RULE, DEALER_TOOL
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.agents import battery_support, motor_support, narrow_support
from emotorad_ai.config import Settings
from emotorad_ai.geo import PincodeCentres
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.dealer_stores import FIXTURE_ROWS, DealerDirectory, StoreCards
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, build_registry

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "411001": (18.52, 73.86), "400054": (19.06, 72.84),
                          "110016": (28.55, 77.20)})
AREA = {"pincode": "411001", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def runtime_with(responses):
    cards = StoreCards()
    directory = DealerDirectory(lambda: [dict(r) for r in FIXTURE_ROWS], CENTRES)
    registry = build_registry(today=date(2026, 10, 9), dealers=directory, store_cards=cards)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      store_cards=cards)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm, cards


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-d").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-d", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class AgentSliceTests(unittest.TestCase):
    def test_the_rules_tool_name_is_the_registrys(self):
        self.assertEqual(DEALER_TOOL, FIND_NEAREST_DEALERS)

    def test_the_three_customer_agents_have_the_tool(self):
        for module in (battery_support, motor_support, narrow_support):
            self.assertIn(FIND_NEAREST_DEALERS, module.TOOL_NAMES)

    def test_the_rule_is_given_only_with_the_tool(self):
        runtime, adapter, llm, _ = runtime_with([say("Hello.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertIn(DEALER_RULE.strip(), llm.requests[0]["system"])
        plain = build_registry(today=date(2026, 10, 9))
        llm2 = ScriptedClaude([say("Hello.")])
        runtime2 = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=plain, llm=llm2,
                           log=EventLog(path=None), resolver=IdentityResolver(plain), self_service_identity=True)
        adapter2 = WebsiteChatAdapter(runtime2.resolver)
        runtime2.conversations.get("conv-d").route_to(AGENT_NAME)
        runtime2.handle(adapter2.to_message({"conversation_id": "conv-d", "session_token": "sess-ananya", "text": "hi"}))
        self.assertNotIn(DEALER_RULE.strip(), llm2.requests[0]["system"])


class CardsTests(unittest.TestCase):
    def test_three_cards_on_the_reply_and_no_phone_in_the_models_context(self):
        runtime, adapter, llm, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}),
            say("The nearest dealer is Test Cycles Pune in Pune. Their details are below."),
        ])
        reply = send(runtime, adapter, "it still won't start, where can I take it?", area=AREA)
        self.assertEqual([s["ref"] for s in reply.stores], ["D1", "D2", "D3"])
        self.assertEqual((reply.stores[0]["name"], reply.stores[0]["phone"]), ("Test Cycles Pune", "+91 9000000001"))
        context = json.dumps(llm.requests, default=str)
        self.assertNotIn("9000000001", context)
        self.assertNotIn("Test Manager One", context)

    def test_cards_never_carry_into_the_next_turn(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}), say("Details below."), say("Anything else?"),
        ])
        self.assertEqual(len(send(runtime, adapter, "where can I take it?", area=AREA).stores), 3)
        self.assertEqual(send(runtime, adapter, "thanks").stores, [])

    def test_leftover_cards_from_a_replaced_reply_are_dropped(self):
        runtime, adapter, _, cards = runtime_with([say("Anything else?")])
        cards.put("conv-d", [{"ref": "D1"}])  # as if a guardrail replaced the turn that found them
        self.assertEqual(send(runtime, adapter, "thanks").stores, [])

    def test_no_area_puts_the_location_button_on_the_reply(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {}), say("What's your pin code? Or tap the button to share your location."),
        ])
        reply = send(runtime, adapter, "where can I take it?")
        self.assertEqual(reply.stores, [])
        self.assertIn({"kind": "request_location", "label": "Share my location"}, reply.actions)

    def test_a_typed_pincode_becomes_the_chats_area(self):
        runtime, adapter, _, _ = runtime_with([
            call_tool(FIND_NEAREST_DEALERS, {"pincode": "400054"}), say("Details below."),
        ])
        reply = send(runtime, adapter, "I'm at 400054 this week", area=AREA)
        self.assertEqual(reply.stores[0]["name"], "Test Cycles Mumbai")
        area = runtime.conversations.get("conv-d").area
        self.assertEqual((area["pincode"], area["source"]), ("400054", "typed"))

    def test_a_safety_report_never_carries_cards(self):
        runtime, adapter, llm, cards = runtime_with([])
        cards.put("conv-d", [{"ref": "D1"}])
        reply = send(runtime, adapter, "my battery is swelling and smoking", area=AREA)
        self.assertEqual(reply.stores, [])
        self.assertEqual(llm.requests, [])


if __name__ == "__main__":
    unittest.main()
