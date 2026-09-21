"""Share my location, end to end on the website chat surface.

The model offers it by calling `offer_location_share`; the reply carries an
action the page renders as a button; the page posts coordinates once; the
endpoint turns them into the customer's own message with the pincode in it.
The model never sees coordinates and nothing stores them.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import OFFER_LOCATION_SHARE, build_registry


def _runtime(responses, location_sharing=True):
    registry = build_registry(today=date(2026, 7, 28), location_sharing=location_sharing)
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry, llm=ScriptedClaude(responses), log=EventLog(path=None),
        resolver=IdentityResolver(registry), self_service_identity=True,
    )
    return runtime, WebsiteChatAdapter(runtime.resolver)


def _send(runtime, adapter, text):
    runtime.conversations.get("conv-1").route_to(AGENT_NAME)
    return runtime.handle(adapter.to_message({"conversation_id": "conv-1", "session_token": "sess-ananya", "text": text}))


class ToolTests(unittest.TestCase):
    def test_absent_unless_the_surface_can_share_a_location(self):
        """WhatsApp has native location sharing and IVR has none; neither gets
        a button that does not exist."""
        self.assertNotIn(OFFER_LOCATION_SHARE, build_registry().specs)

    def test_present_when_it_can(self):
        self.assertIn(OFFER_LOCATION_SHARE, build_registry(location_sharing=True).specs)

    def test_the_self_service_agent_can_see_it(self):
        runtime, _ = _runtime([say("hi")])
        self.assertIn(OFFER_LOCATION_SHARE, runtime.agents[AGENT_NAME].definition.tool_names)

    def test_the_production_slice_does_not_carry_it(self):
        runtime, _ = _runtime([say("hi")], location_sharing=False)
        self.assertNotIn(OFFER_LOCATION_SHARE, runtime.agents[AGENT_NAME].definition.tool_names)


class ReplyTests(unittest.TestCase):
    def test_the_offer_reaches_the_reply_as_an_action(self):
        runtime, adapter = _runtime([
            call_tool(OFFER_LOCATION_SHARE, {}),
            say("What's the pincode? Or tap the button to share your location."),
        ])
        reply = _send(runtime, adapter, "send it to a new address")
        self.assertEqual(reply.actions, [{"kind": "request_location", "label": "Share my location"}])

    def test_a_reply_without_the_offer_carries_no_action(self):
        runtime, adapter = _runtime([say("Hello.")])
        self.assertEqual(_send(runtime, adapter, "hi").actions, [])


class EndpointTests(unittest.TestCase):
    """The HTTP surface: coordinates in, the customer's message out."""

    def setUp(self):
        from fastapi.testclient import TestClient
        import emotorad_ai.api as api

        self.api = api
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.original = api.geocoder

    def tearDown(self):
        self.api.geocoder = self.original

    def test_a_location_becomes_the_customers_message_with_the_pincode(self):
        class Fake:
            def reverse(self, lat, lon):
                return {"postcode": "122018", "suburb": "Sector 49"}

        self.api.geocoder = Fake()
        response = self.client.post("/message", json={
            "em_aid": "aid-loc", "agent": "battery_support", "text": "",
            "location": {"latitude": 28.41, "longitude": 77.05},
        })
        self.assertEqual(response.status_code, 200, response.text)
        inbound = [e for e in self.api.log.events if e["event"] == "inbound"][-1]
        self.assertEqual(inbound["text"], "I shared my location. Pincode 122018, Sector 49, Gurugram, Haryana.")

    def test_an_unresolved_location_says_so_and_never_leaks_coordinates(self):
        class Down:
            def reverse(self, lat, lon):
                raise RuntimeError("down")

        self.api.geocoder = Down()
        with self.assertLogs("emotorad_ai.location", level="WARNING"):
            response = self.client.post("/message", json={
                "em_aid": "aid-loc2", "agent": "battery_support", "text": "",
                "location": {"latitude": 28.41, "longitude": 77.05},
            })
        self.assertEqual(response.status_code, 200, response.text)
        inbound = [e for e in self.api.log.events if e["event"] == "inbound"][-1]
        self.assertIn("could not be worked out", inbound["text"])
        for event in self.api.log.events:
            self.assertNotIn("28.41", str(event))

    def test_the_reply_shape_carries_actions(self):
        response = self.client.post("/message", json={"em_aid": "aid-shape", "text": "hi"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("actions", response.json())

    def test_a_malformed_location_is_refused(self):
        response = self.client.post("/message", json={
            "em_aid": "aid-bad", "text": "", "location": {"latitude": 200, "longitude": 77.05},
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
