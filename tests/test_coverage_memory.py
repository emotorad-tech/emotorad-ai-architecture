"""A coverage fact established earlier in the conversation still counts.

Reported from a phone on 20 September, conversation 3fde2772. The customer
verified, the bot looked up their bike (in warranty until 2027), diagnosed a
melted battery terminal from two photos over the next three turns, opened a
ticket, and then said the replacement was covered — which it was. The coverage
post-check blocked that reply as `coverage_claim_without_tool_result` and
escalated the customer to a human "so they can check your warranty", when the
warranty had been checked three turns earlier and was sitting in the history.

The post-check was fed only the current turn's tool results. Coverage is looked
up once per conversation and then relied on, so a correct claim on any later
turn was a false positive, and the flow that matters most — "your battery is
covered, here is what happens next" — could never complete unless the model
happened to re-run the lookup in the same breath.

The control itself is untouched: a claim must still match what a tool returned.
The tool result is just remembered for the conversation instead of the turn.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry


def _runtime(responses):
    registry = build_registry(today=date(2026, 7, 28))
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=ScriptedClaude(responses),
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime, WebsiteChatAdapter(runtime.resolver)


def _send(runtime, adapter, text, conversation_id="conv-1"):
    runtime.conversations.get(conversation_id).route_to("battery_support")
    message = adapter.to_message(
        {"conversation_id": conversation_id, "session_token": "sess-ananya", "text": text}
    )
    return runtime.handle(message)


class CoverageRememberedAcrossTurnsTests(unittest.TestCase):
    def test_a_correct_claim_on_a_later_turn_is_not_blocked(self):
        """The shape of conversation 3fde2772, compressed to two turns."""
        runtime, adapter = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found your EMX Plus."),
            say("The terminal is heat damaged. Good news: the battery is covered under warranty, so the replacement is free."),
        ])
        _send(runtime, adapter, "my battery terminal looks melted")
        reply = _send(runtime, adapter, "here is the second photo")
        self.assertFalse(reply.escalated, reply.text)
        self.assertIn("covered under warranty", reply.text)

    def test_a_claim_with_no_lookup_anywhere_is_still_blocked(self):
        """The Air Canada case is unchanged: no tool ever said so, no claim."""
        runtime, adapter = _runtime([
            say("Hello."),
            say("Don't worry, that's covered under warranty."),
        ])
        _send(runtime, adapter, "hi")
        reply = _send(runtime, adapter, "is it covered?")
        self.assertTrue(reply.escalated)

    def test_a_claim_contradicting_the_earlier_lookup_is_still_blocked(self):
        """Remembering the fact must not weaken the check against it."""
        runtime, adapter = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found your EMX Plus."),
            say("Unfortunately the battery is not covered, so this would be chargeable."),
        ])
        _send(runtime, adapter, "my battery is dead")
        reply = _send(runtime, adapter, "is it covered?")
        self.assertTrue(reply.escalated)

    def test_the_fact_is_scoped_to_its_own_conversation(self):
        """One customer's lookup must never vouch for another's claim."""
        runtime, adapter = _runtime([
            call_tool("lookup_warranty_record", {}),
            say("Found it."),
            say("That's covered under warranty."),
        ])
        _send(runtime, adapter, "hi", conversation_id="conv-A")
        reply = _send(runtime, adapter, "is mine covered?", conversation_id="conv-B")
        self.assertTrue(reply.escalated)


class BlockedRepliesAreVisibleTests(unittest.TestCase):
    def test_what_was_blocked_is_written_to_the_log(self):
        """Without this, a false positive and a true positive look identical in
        the log, and the only way to tell them apart is to ask the customer
        what they nearly got told."""
        runtime, adapter = _runtime([
            say("Hello."),
            say("Don't worry, that's covered under warranty."),
        ])
        _send(runtime, adapter, "hi")
        _send(runtime, adapter, "is it covered?")
        blocked = [e for e in runtime.log.events if e.get("event") == "guardrail_triggered"]
        self.assertTrue(blocked)
        self.assertIn("covered under warranty", str(blocked[-1]))


if __name__ == "__main__":
    unittest.main()
