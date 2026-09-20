"""The HTTP surface `/chat` talks to, and the ways a caller can break it.

`agent` is the one field on `MessageIn` that reaches into the runtime's own
wiring, and it arrives from the browser. Everything here is about that field
being wrong, either by accident or on purpose.
"""

import unittest

from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry


def _runtime(responses=None):
    registry = build_registry()
    return Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=ScriptedClaude(responses if responses is not None else [say("Hello.")]),
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )


def _message(conversation_id="conv-1", text="hello"):
    return InboundMessage(
        conversation_id=conversation_id,
        persona="customer",
        identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
        channel="website_chat",
        message_text=text,
    )


class UnknownPinnedAgentTests(unittest.TestCase):
    """A conversation must never be left unable to continue.

    Observed 2026-09-20: posting `agent: "no_such_agent"` returned HTTP 500, and
    so did every later turn on that conversation id — including turns that sent
    no agent at all. `route_to` had written the bad name into the conversation
    state, `state.agent` was no longer None so triage never ran, and
    `self.agents[state.agent]` raised KeyError forever. The customer saw a
    network error on every message with no way back.
    """

    def test_a_conversation_pinned_to_an_unknown_agent_still_answers(self):
        runtime = _runtime()
        runtime.conversations.get("conv-1").route_to("no_such_agent")
        reply = runtime.handle(_message())
        self.assertTrue(reply.text)

    def test_it_recovers_rather_than_staying_stuck(self):
        """The bad pin must be cleared, not merely survived once."""
        runtime = _runtime([say("First."), say("Second.")])
        runtime.conversations.get("conv-1").route_to("no_such_agent")
        runtime.handle(_message())
        self.assertIsNone(runtime.conversations.get("conv-1").agent)

    def test_a_real_agent_is_still_pinned(self):
        runtime = _runtime()
        runtime.conversations.get("conv-1").route_to("battery_support")
        runtime.handle(_message())
        self.assertEqual(runtime.conversations.get("conv-1").agent, "battery_support")


class PinnedAgentIsValidatedAtTheSurfaceTests(unittest.TestCase):
    """`agent` comes from the browser, so the surface decides what it may name.

    Dropping a bad pin inside the runtime keeps the conversation alive, but the
    caller should still be told they asked for something that does not exist
    rather than quietly getting triage. And the website must not be able to run
    the dealer agent: its tools are unreachable without a dealer_id, so nothing
    leaks, but it would spend tokens answering a customer in a dealer's voice.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        import emotorad_ai.api as api

        self.api = api
        self.client = TestClient(api.app, raise_server_exceptions=False)

    def test_an_unknown_agent_is_refused(self):
        response = self.client.post(
            "/message",
            json={"text": "hello", "em_aid": "aid-1", "agent": "no_such_agent"},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_the_dealer_agent_is_not_reachable_from_website_chat(self):
        response = self.client.post(
            "/message",
            json={"text": "hello", "em_aid": "aid-1", "agent": "dealer_orders"},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_a_refused_pin_does_not_poison_the_conversation(self):
        """The 500 loop began because the bad name was written before it was
        checked. Nothing may be written until it has been."""
        self.client.post(
            "/message",
            json={
                "text": "hello",
                "em_aid": "aid-1",
                "agent": "no_such_agent",
                "conversation_id": "conv-poison",
            },
        )
        self.assertIsNone(self.api.runtime.conversations.get("conv-poison").agent)

    def test_a_customer_agent_is_accepted(self):
        self.assertIn("battery_support", self.api.CHAT_AGENTS)
        self.assertNotIn("dealer_orders", self.api.CHAT_AGENTS)


class TheChatAgentCanActuallySendPicturesTests(unittest.TestCase):
    """`send_guide_media` was in the tool slice and not in the registry.

    `TOOL_NAMES` named it, but `_build_registry` never passed a catalogue, so
    the tool was never registered and `Agent.run` filtered it straight back out
    of the slice. The model was told in its prompt to show people where a button
    is, and had no way to show them anything. Pointing at a part is most of what
    this agent does.
    """

    def setUp(self):
        import emotorad_ai.api as api

        self.api = api

    def test_the_tool_is_registered(self):
        self.assertIn("send_guide_media", self.api.registry.specs)

    def test_the_agent_can_see_it(self):
        names = self.api.runtime.agents["battery_support"].definition.tool_names
        visible = [n for n in names if n in self.api.registry.specs]
        self.assertIn("send_guide_media", visible)

    def test_the_catalogue_is_not_empty(self):
        """An empty catalogue registers a tool that can send nothing, which is
        worse than no tool: the model keeps trying."""
        self.assertTrue(self.api.GUIDE_MEDIA)


class MessageRateLimitTests(unittest.TestCase):
    """The endpoint refuses rather than spends, once a caller goes too fast."""

    def setUp(self):
        from fastapi.testclient import TestClient
        import emotorad_ai.api as api

        self.api = api
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.original = api.message_limiter

    def tearDown(self):
        self.api.message_limiter = self.original

    def test_a_flood_is_refused_with_429(self):
        from emotorad_ai.ratelimit import RateLimiter

        self.api.message_limiter = RateLimiter(limit=2, window_seconds=60.0)
        codes = [
            self.client.post("/message", json={"text": "hi", "em_aid": "aid-flood"}).status_code
            for _ in range(4)
        ]
        self.assertEqual(codes[-1], 429, codes)

    def test_the_limit_is_not_applied_to_a_normal_exchange(self):
        response = self.client.post("/message", json={"text": "hi", "em_aid": "aid-normal"})
        self.assertNotEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
