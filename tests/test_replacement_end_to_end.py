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
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.verification import VerificationStore

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
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "conv-1-battery"}),
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
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "k"}),
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

    def test_lookup_and_order_in_one_turn_succeeds(self):
        """A model that calls lookup_warranty_record and place_replacement_order
        in the same assistant turn must see the lookup's own result when the
        order tool asks for coverage_result — not `missing_identity`, because
        the lookup has not "finished" in the sense of the turn ending yet."""
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            call_tool(
                PLACE_REPLACEMENT_ORDER,
                {"part": "battery", "use_record_address": True, "idempotency_key": "one-turn"},
            ),
            say("Done. Order RO-00001 is on its way."),
        ])
        reply = _send(runtime, adapter, "please replace my battery, here is the photo", photo=True)
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        self.assertIsNotNone(orders.in_flight("EMXP2025004417", "battery"))

    def test_repeating_the_order_id_next_turn_is_not_blocked(self):
        """A placed order is remembered for the whole conversation, so a
        customer asking about it later must not trip the order post-check."""
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it. Address still right: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "a"}),
            say("Order RO-00001 placed."),
            say("It was RO-00001."),
        ])
        _send(runtime, adapter, "melted terminal", photo=True)
        _send(runtime, adapter, "yes")
        reply = _send(runtime, adapter, "It was RO-00001.")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)

    def test_an_order_id_from_another_conversation_is_still_blocked(self):
        orders = ReplacementOrders()
        registry = build_registry(
            today=date(2026, 7, 28), replacement_orders=orders, item_codes=ItemCodes(), approval_mode="reasonable",
        )
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False, approval_mode="reasonable"),
            registry=registry,
            llm=ScriptedClaude([
                call_tool("lookup_warranty_record", {}),
                say("Found it. Address still right: " + ADDRESS + "?"),
                call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "a"}),
                say("Order RO-00001 placed."),
                say("Your order RO-00001 is on its way."),
            ]),
            log=EventLog(path=None),
            resolver=IdentityResolver(registry),
        )
        adapter = WebsiteChatAdapter(runtime.resolver)

        def send(conversation_id, text, photo=False):
            runtime.conversations.get(conversation_id).route_to(AGENT_NAME)
            event = {"conversation_id": conversation_id, "session_token": "sess-ananya", "text": text}
            if photo:
                event["attachments"] = [{"kind": "image", "url": _JPEG}]
            return runtime.handle(adapter.to_message(event))

        send("conv-a", "melted terminal", photo=True)
        send("conv-a", "yes")

        reply = send("conv-b", "Your order RO-00001 is on its way.")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "guardrail:order_post_check")

    def test_asking_again_tomorrow_does_not_place_a_second_order(self):
        runtime, adapter, orders = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it. Address still right: " + ADDRESS + "?"),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "a"}),
            say("Order RO-00001 placed."),
            call_tool(PLACE_REPLACEMENT_ORDER, {"part": "battery", "use_record_address": True, "idempotency_key": "b"}),
            say("That is already on its way as RO-00001."),
        ])
        _send(runtime, adapter, "melted terminal", photo=True)
        _send(runtime, adapter, "yes")
        reply = _send(runtime, adapter, "did that go through? send me a battery")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        self.assertEqual(len(orders._orders), 1)


class AddressFromTheConversationTests(unittest.TestCase):
    """Final fix wave, item 1 (2026-09-20): a spent one-time code must not
    count as part of an address.

    On the self-service identity path the customer types their OTP as a plain
    message before the model calls `verify_identity`. Nine in ten OTPs start
    with a non-zero digit, so the six-digit run reads exactly like a pincode,
    and `place_replacement_order`'s provenance check used to accept it as
    something the customer had typed. `_remember_lookup` now tracks codes
    `verify_identity` accepted on `state.consumed_codes` and the
    `customer_messages` fact subtracts them, so the spent code is no longer
    address text the tool can be fed back.
    """

    @staticmethod
    def _self_service_runtime(responses):
        orders = ReplacementOrders()
        store = VerificationStore()
        registry = build_registry(
            today=date(2026, 7, 28), replacement_orders=orders, item_codes=ItemCodes(),
            verification=store,
        )
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry, llm=ScriptedClaude(responses), log=EventLog(path=None),
            resolver=IdentityResolver(registry),
            self_service_identity=True,
            phone_resolver=store.verified_phone,
        )
        return runtime, store, orders

    @staticmethod
    def _send_anonymous(runtime, conversation_id, text):
        runtime.conversations.get(conversation_id).route_to(AGENT_NAME)
        message = InboundMessage(
            conversation_id=conversation_id,
            persona="customer",
            identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
            channel="website_chat",
            message_text=text,
        )
        return runtime.handle(message)

    def test_a_verified_code_is_not_a_pincode(self):
        # The brief's script calls `place_replacement_order` straight after
        # `verify_identity`, but the tool also injects `coverage_result`,
        # which is only ever set by a `lookup_warranty_record` call the
        # runtime remembered; without one the registry refuses the order as
        # `missing_fact` before the address is ever inspected. A
        # `lookup_warranty_record` call is added in the same turn as
        # `verify_identity` (the pattern `VerifyThenLookUpInOneTurnTests`
        # already exercises) so the probe actually reaches the address check
        # the brief is testing.
        runtime, store, orders = self._self_service_runtime(
            [
                call_tool("verify_identity", {"code": "482913"}),
                call_tool("lookup_warranty_record", {}),
                say("Verified. What's the address?"),
                call_tool(
                    PLACE_REPLACEMENT_ORDER,
                    {
                        "part": "battery",
                        "address": {"house_or_flat": "A1102", "building_or_street": "Park view city 1",
                                    "area": "Sector 49", "pincode": "482913"},
                        "idempotency_key": "c1",
                    },
                ),
                say("Done, order RO-00001."),
            ]
        )
        store.issue("conv-1", "+919876543210", "482913")

        self._send_anonymous(runtime, "conv-1", "482913")
        self._send_anonymous(runtime, "conv-1", "It's A1102 Park view city 1")

        order_calls = [
            event
            for event in runtime.log.events
            if event["event"] == "tool_call" and event["tool"] == PLACE_REPLACEMENT_ORDER
        ]
        self.assertEqual(len(order_calls), 1, runtime.log.events)
        error = order_calls[0]["result"].get("error", {})
        self.assertEqual(error.get("code"), "address_unconfirmed", order_calls[0])
        self.assertIn("482913", error.get("message", ""))
        self.assertIsNone(orders.in_flight("EMXP2025004417", "battery"))

    def test_an_address_given_across_two_turns_with_a_read_back_is_accepted(self):
        # A `lookup_warranty_record` call is added on turn one, for the same
        # reason as above: `coverage_result` must be set before
        # `place_replacement_order` will run at all.
        runtime, adapter, orders = _runtime(
            [
                call_tool("lookup_warranty_record", {}),
                say("And the pincode?"),
                say("So that's A1102 Park view city 1, 122018, is that right?"),
                call_tool(
                    PLACE_REPLACEMENT_ORDER,
                    {
                        "part": "battery",
                        "address": {"house_or_flat": "A1102", "building_or_street": "Park view city 1",
                                    "area": "Sector 49", "pincode": "122018"},
                        "idempotency_key": "EMXP2025004417-battery",
                    },
                ),
                say("Done. Order RO-00001 is on its way."),
            ]
        )
        _send(runtime, adapter, "It's A1102 Park view city 1, Sector 49", photo=True)
        _send(runtime, adapter, "122018")
        reply = _send(runtime, adapter, "Yes")

        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("RO-00001", reply.text)
        placed = orders.in_flight("EMXP2025004417", "battery")
        self.assertIsNotNone(placed)
        self.assertEqual(placed["delivery_address"], "A1102, Park view city 1, Sector 49, Gurugram, Haryana, 122018")


if __name__ == "__main__":
    unittest.main()
