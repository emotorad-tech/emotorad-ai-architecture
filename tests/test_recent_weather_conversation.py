"""Recent weather through runtime.handle() (spec 2026-10-09 recent weather, sections 4 and 5)."""

import json
import unittest
from dataclasses import replace
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents import battery_support, motor_support, narrow_support
from emotorad_ai.agents.base import WEATHER_RULE, WEATHER_TOOL
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import GET_RECENT_WEATHER, build_registry
from emotorad_ai.weather import WeatherReport

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}
REPORT = WeatherReport(current_c=39.6, days=tuple(
    {"days_ago": n, "max_c": 41.0 if n < 3 else 33.0, "min_c": 26.0} for n in range(15)))


class FakeWeather:
    def __init__(self):
        self.calls = []

    def recent(self, pincode, point):
        self.calls.append(pincode)
        return REPORT


def runtime_with(responses, weather=True):
    registry = build_registry(today=date(2026, 10, 9), weather=FakeWeather() if weather else None)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, area=None):
    runtime.conversations.get("conv-w").route_to(AGENT_NAME)
    message = adapter.to_message({"conversation_id": "conv-w", "session_token": "sess-ananya", "text": text})
    if area is not None:
        message = replace(message, entry_metadata=dict(message.entry_metadata, area=area))
    return runtime.handle(message)


class SliceTests(unittest.TestCase):
    def test_the_battery_and_narrow_agents_have_it_and_the_motor_agent_not(self):
        self.assertEqual(WEATHER_TOOL, GET_RECENT_WEATHER)
        self.assertIn(GET_RECENT_WEATHER, battery_support.TOOL_NAMES)
        self.assertIn(GET_RECENT_WEATHER, narrow_support.TOOL_NAMES)
        self.assertNotIn(GET_RECENT_WEATHER, motor_support.TOOL_NAMES)

    def test_the_rule_comes_only_with_the_tool(self):
        runtime, adapter, llm = runtime_with([say("Hello.")])
        send(runtime, adapter, "hi", area=AREA)
        self.assertIn(WEATHER_RULE.strip(), llm.requests[0]["system"])
        runtime2, adapter2, llm2 = runtime_with([say("Hello.")], weather=False)
        send(runtime2, adapter2, "hi", area=AREA)
        self.assertNotIn(WEATHER_RULE.strip(), llm2.requests[0]["system"])


class RuleWordingTests(unittest.TestCase):
    """The final review's I1: the battery safety gate stops a chat on 'very hot' or
    'extremely hot' (guardrails._SAFETY_TERMS, Tier-1, unchanged). The rule must not
    put those words in the bot's mouth, or invite them as the rider's answer."""

    def test_the_question_asks_about_warmth_not_heat(self):
        self.assertNotIn("that hot", WEATHER_RULE)
        self.assertIn("about that warm", WEATHER_RULE)
        self.assertIn('Never say "very hot" or "extremely hot"', WEATHER_RULE)

    def test_a_rider_who_answers_very_hot_still_meets_the_safety_gate(self):
        """Pinned on purpose: the gate wins over the weather question. Raise
        with Sachin before ever changing it."""
        runtime, adapter, llm = runtime_with([])
        reply = send(runtime, adapter, "yes very hot", area=AREA)
        self.assertTrue(reply.handled_by.startswith("guardrail"), reply.handled_by)
        self.assertEqual(llm.requests, [])


class ConversationTests(unittest.TestCase):
    def test_the_agent_states_the_weather_and_asks_once(self):
        runtime, adapter, llm = runtime_with([
            call_tool(GET_RECENT_WEATHER, {}),
            say("It has been up to 41 °C around Pune this week. Is it about that warm where you charge it, or cooler indoors?"),
        ])
        reply = send(runtime, adapter, "my battery won't charge", area=AREA)
        self.assertIn("41 °C", reply.text)
        self.assertFalse(reply.handled_by.startswith("guardrail"), reply.handled_by)
        result = json.dumps(llm.requests[1]["messages"][-1], default=str)
        self.assertIn("days_above_40c", result)
        self.assertNotIn("2026-", result)

    def test_a_typed_pincode_becomes_the_chats_area(self):
        runtime, adapter, _ = runtime_with([
            call_tool(GET_RECENT_WEATHER, {"pincode": "400054"}), say("Is it about that warm where you charge it, or cooler indoors?"),
        ])
        send(runtime, adapter, "I'm at 400054 this week, it won't charge", area=AREA)
        area = runtime.conversations.get("conv-w").area
        self.assertEqual((area["pincode"], area["source"]), ("400054", "typed"))
        self.assertTrue(area.get("at"))


if __name__ == "__main__":
    unittest.main()
