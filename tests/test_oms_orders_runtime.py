"""A rider whose only bike is on an OMS order, through runtime.handle()
(spec 2026-10-10, section 7)."""

import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import build_registry
from tests.test_oms_orders import FakeConnection

TODAY = date(2026, 10, 10)
ORDER = {"frame_number": "EMORD0001", "product_name": "EMX+ Red White - M042BV01C52",
         "purchase_date": date(2026, 7, 13), "order_source": "End Customer"}


def make(responses, orders=(ORDER,), on=True):
    db = oms_db.OMSDatabase("postgresql://ro@oms/emotorad", connect=FakeConnection(orders=orders),
                            today=lambda: TODAY, orders=on)
    registry = build_registry(today=TODAY, warranty_source=oms_db.db_warranty_source(db))
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      warranty_step=True)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, seen=False):
    state = runtime.conversations.get("conv-ord")
    state.route_to(BATTERY)
    state.selected_frame = "EMORD0001"
    state.evidence_seen = seen
    return runtime.handle(adapter.to_message({"conversation_id": "conv-ord", "session_token": "sess-ananya",
                                              "text": text}))


class OrderBikeTests(unittest.TestCase):
    def test_the_order_bike_is_the_riders_bike(self):
        runtime, adapter, llm = make([say("Let's check the charger first.")])
        send(runtime, adapter, "my battery won't charge")
        self.assertIn("EMORD0001", llm.requests[0]["system"])

    def test_the_warranty_step_is_case_one_not_unregistered(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault in your video.")])
        reply = send(runtime, adapter, "here is the video", seen=True)
        state = runtime.conversations.get("conv-ord")
        self.assertEqual(state.warranty_step_frames, ["EMORD0001"])
        self.assertIn("from_warranty_api", json.dumps(state.coverage_result))
        self.assertNotIn("isn't registered", reply.text)
        self.assertEqual(reply.actions, [])

    def test_with_the_switch_off_the_rider_has_no_bike(self):
        runtime, adapter, llm = make([say("Tell me what is happening.")], on=False)
        send(runtime, adapter, "my battery won't charge")
        self.assertNotIn("EMORD0001", llm.requests[0]["system"])
