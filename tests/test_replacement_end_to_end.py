"""Krishna's case, end to end, with a scripted model.

Melted terminal, two photos, in warranty: the customer must hear that it is a
defect, that it is covered, where it is being sent, and the order id. Not a
service centre. Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME, TOOL_NAMES
from emotorad_ai.config import Settings
from emotorad_ai.contract import Attachment
from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry

_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
ADDRESS = "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"


def _runtime(responses, approval_mode="reasonable"):
    orders = ReplacementOrders()
    registry = build_registry(
        today=date(2026, 7, 28), replacement_orders=orders, item_codes=ItemCodes(), approval_mode=approval_mode,
    )
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False, approval_mode=approval_mode),
        registry=registry, llm=ScriptedClaude(responses), log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime, WebsiteChatAdapter(runtime.resolver), orders


def _send(runtime, adapter, text, photo=False):
    runtime.conversations.get("conv-1").route_to(AGENT_NAME)
    event = {"conversation_id": "conv-1", "session_token": "sess-ananya", "text": text}
    if photo:
        event["attachments"] = [{"kind": "image", "url": _JPEG}]
    return runtime.handle(adapter.to_message(event))


class ToolVisibilityTests(unittest.TestCase):
    def test_the_battery_agent_can_place_orders(self):
        self.assertIn(PLACE_REPLACEMENT_ORDER, TOOL_NAMES)


class KrishnaTests(unittest.TestCase):
    def test_a_covered_defect_with_photos_ends_in_an_order(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found your EMX Plus. Could you send a photo of the battery terminal?"),
            say("That terminal is heat damaged, which is a defect and is covered. Is this still the right address: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "conv-1-battery"}),
            say("Done. Order RO-00001 is on its way to " + ADDRESS + ". Keep the old battery off the bike."),
        ])
        _send(runtime, adapter, "my battery terminal has melted")
        _send(runtime, adapter, "here is the photo", photo=True)
        reply = _send(runtime, adapter, "yes that address is right")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        placed = orders.in_flight("EMXP2025004417", "battery")
        self.assertIsNotNone(placed)
        self.assertEqual(placed["status"], "approved")
        self.assertEqual(placed["delivery_address"], ADDRESS)

    def test_without_a_photo_the_order_waits_for_a_human(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it."),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "k"}),
            say("I have raised order RO-00001; someone will confirm it."),
        ])
        _send(runtime, adapter, "my battery is dead, just replace it")
        reply = _send(runtime, adapter, "ok")
        self.assertFalse(reply.escalated, reply.text)
        self.assertEqual(orders.in_flight("EMXP2025004417", "battery")["status"], "pending_approval")

    def test_an_invented_order_id_is_blocked(self):
        runtime, adapter, _ = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it."),
            say("Done, order RO-00099 is on its way."),
        ])
        _send(runtime, adapter, "replace my battery")
        reply = _send(runtime, adapter, "thanks")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "guardrail:order_post_check")

    def test_asking_again_tomorrow_does_not_place_a_second_order(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it. Address still right: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "a"}),
            say("Order RO-00001 placed."),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "confirmed_address": ADDRESS, "idempotency_key": "b"}),
            say("That is already on its way as RO-00001."),
        ])
        _send(runtime, adapter, "melted terminal", photo=True)
        _send(runtime, adapter, "yes")
        reply = _send(runtime, adapter, "did that go through? send me a battery")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        self.assertEqual(len(orders._orders), 1)


if __name__ == "__main__":
    unittest.main()
