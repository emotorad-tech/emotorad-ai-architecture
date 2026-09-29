import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.decisions import Q_CATEGORY, Q_LANGUAGE, Q_STANDARD, Q_SUB_CATEGORY, Q_WARRANTY
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, JevError, ScriptedJev, choose, yes
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterUnavailable
from emotorad_ai.runtime import Runtime
from emotorad_ai.standard_responses import StandardResponse
from emotorad_ai.tools.mocks import build_registry

TODAY = date(2026, 7, 28)
THANKS = StandardResponse("std-thanks", "approved", "K", "Only thanks.", {"english": "You're welcome, glad that helped."})

NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
NARROW_WITH_WARRANTY = dict(NARROW, **{Q_WARRANTY: yes(0.9)})
UNSURE = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose("none", 0.5)}


class RaisingLLM:
    def __init__(self):
        self.requests = []

    def create(self, system, messages, tools):
        self.requests.append({"system": system, "messages": list(messages)})
        raise OpenRouterUnavailable("down")


def build(jev_decisions, fallback=(), narrow=(), narrow_llm=None, fallback_llm=None):
    registry = build_registry(today=TODAY)
    jev = ScriptedJev(jev_decisions)
    fallback_llm = fallback_llm or ScriptedClaude(list(fallback))
    narrow_llm = narrow_llm or ScriptedClaude(list(narrow))
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=fallback_llm,
        narrow_llm=narrow_llm,
        jev=jev,
        standard_responses=[THANKS],
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime, WebsiteChatAdapter(runtime.resolver), jev, narrow_llm, fallback_llm


def send(runtime, adapter, text, session="sess-ananya", attachments=()):
    return runtime.handle(adapter.to_message(
        {"conversation_id": "conv-1", "session_token": session, "text": text, "attachments": list(attachments)}
    ))


class GatesRunBeforeJevTests(unittest.TestCase):
    def test_a_safety_trigger_calls_neither_jev_nor_any_model(self):
        runtime, adapter, jev, narrow, fallback = build([])
        reply = send(runtime, adapter, "my battery is swollen")
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertEqual((jev.calls, narrow.requests, fallback.requests), ([], [], []))

    def test_a_request_for_a_human_calls_neither(self):
        runtime, adapter, jev, narrow, fallback = build([])
        send(runtime, adapter, "I want to talk to a human")
        self.assertEqual((jev.calls, narrow.requests, fallback.requests), ([], [], []))


class StandardPathTests(unittest.TestCase):
    def test_a_confident_standard_reply_uses_no_model_and_keeps_the_disclosure(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers={Q_STANDARD: choose("std-thanks", 0.97), Q_LANGUAGE: choose("english", 0.95)})]
        )
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("You're welcome, glad that helped.", reply.text)
        self.assertTrue(has_disclosure(reply.text))
        self.assertEqual(reply.handled_by, "standard:std-thanks")
        self.assertEqual((narrow.requests, fallback.requests), ([], []))


class NarrowPathTests(unittest.TestCase):
    def test_a_confident_issue_goes_to_the_narrow_model_with_one_record(self):
        runtime, adapter, jev, narrow, fallback = build([JevDecision(answers=NARROW)], narrow=[say("Try another wall socket first.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertIn("Try another wall socket first.", reply.text)
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertEqual(reply.metadata.get("route"), "narrow")
        self.assertEqual(len(narrow.requests), 1)
        self.assertEqual(fallback.requests, [])
        self.assertIn("Battery will not charge", narrow.requests[0]["system"])
        self.assertNotIn("Range has dropped", narrow.requests[0]["system"])

    def test_the_prefetched_warranty_result_satisfies_the_coverage_check(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW_WITH_WARRANTY)], narrow=[say("Good news, it's covered under warranty.")])
        reply = send(runtime, adapter, "my battery won't charge, is it under warranty")
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertIn("lookup_warranty_record", reply.metadata["tool_calls"])

    def test_a_narrow_coverage_claim_that_contradicts_the_prefetch_is_blocked(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW_WITH_WARRANTY)], narrow=[say("Good news, it's covered under warranty.")])
        reply = send(runtime, adapter, "my battery won't charge, is it under warranty", session="sess-rohit")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        # Contradicted, not merely unsupported: the prefetched lookup reached the check.
        self.assertEqual(reply.metadata["blocked_reason"], "coverage_claim_contradicts_tool_result")

    def test_an_unsure_follow_up_stays_on_the_same_record(self):
        runtime, adapter, jev, narrow, fallback = build(
            [JevDecision(answers=NARROW), JevDecision(answers=UNSURE)],
            narrow=[say("Is the charger light red?"), say("Red means it is charging. Leave it for four hours.")],
        )
        send(runtime, adapter, "my battery won't charge")
        reply = send(runtime, adapter, "yes it is red now")
        self.assertIn("Leave it for four hours", reply.text)
        self.assertEqual(len(narrow.requests), 2)
        self.assertIn("Battery will not charge", narrow.requests[1]["system"])
        self.assertEqual(fallback.requests, [])

    def test_a_photo_with_no_text_mid_flow_skips_jev_and_stays_on_the_record(self):
        runtime, adapter, jev, narrow, _ = build(
            [JevDecision(answers=NARROW)], narrow=[say("Please send a photo of the port."), say("Thanks, the port looks bent.")]
        )
        send(runtime, adapter, "my battery won't charge")
        reply = send(runtime, adapter, "", attachments=[{"kind": "image", "url": "https://example.test/port.jpg"}])
        self.assertEqual(len(jev.calls), 1)
        self.assertIn("the port looks bent", reply.text)


class FullPathTests(unittest.TestCase):
    def test_low_confidence_goes_to_the_fallback_model(self):
        runtime, adapter, jev, narrow, fallback = build([JevDecision(answers=UNSURE)], fallback=[say("Tell me more.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual((len(fallback.requests), narrow.requests), (1, []))

    def test_a_jev_failure_goes_to_the_fallback_model_and_is_logged(self):
        runtime, adapter, jev, narrow, fallback = build([JevError("unavailable", "down")], fallback=[say("Tell me more.")])
        send(runtime, adapter, "my battery won't charge")
        self.assertEqual(len(fallback.requests), 1)
        [event] = [e for e in runtime.log.events if e["event"] == "jev_decision"]
        self.assertEqual((event["path"], event["error"]), ("full", "unavailable"))

    def test_a_narrow_model_failure_rolls_history_back_before_the_full_agent(self):
        raising = RaisingLLM()
        runtime, adapter, jev, _, fallback = build([JevDecision(answers=NARROW)], fallback=[say("Tell me more.")], narrow_llm=raising)
        send(runtime, adapter, "my battery won't charge")
        self.assertEqual(len(raising.requests), 1)
        user_turns = [m for m in fallback.requests[0]["messages"] if m.get("content") == "my battery won't charge"]
        self.assertEqual(len(user_turns), 1)

    def test_a_full_agent_model_failure_hands_over(self):
        runtime, adapter, *_ = build([JevDecision(answers=UNSURE)], fallback_llm=RaisingLLM())
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "llm_error")

    def test_a_confident_motor_category_moves_the_conversation_to_the_motor_agent(self):
        runtime, adapter, *_ = build(
            [JevDecision(answers={Q_CATEGORY: choose("motor", 0.95), Q_SUB_CATEGORY: choose("none", 0.9)})],
            fallback=[say("Does the noise change with speed?")],
        )
        # Triage's keywords route this to battery ("won't start"); Jev's confident
        # motor answer is what moves it. A message naming both topics would not
        # reach Jev at all: triage asks which one first, on purpose.
        reply = send(runtime, adapter, "my bike won't start when I pedal")
        self.assertEqual(reply.handled_by, "motor_support")


class JevLogTests(unittest.TestCase):
    def test_the_decision_is_logged_with_scores_and_no_message_text(self):
        runtime, adapter, *_ = build([JevDecision(answers=NARROW, model="typesafe/jev-1.13-x", cost=0.00002)], narrow=[say("Ok.")])
        send(runtime, adapter, "my battery won't charge")
        [event] = [e for e in runtime.log.events if e["event"] == "jev_decision"]
        self.assertEqual(event["path"], "narrow")
        self.assertEqual(event["scores"][Q_CATEGORY]["choice"], "battery")
        self.assertEqual((event["model"], event["cost"]), ("typesafe/jev-1.13-x", 0.00002))
        self.assertNotIn("won't charge", json.dumps(event))

    def test_jev_receives_no_phone_or_name(self):
        runtime, adapter, jev, *_ = build([JevDecision(answers=UNSURE)], fallback=[say("Ok.")])
        # Not "call me": that phrase is a handoff request and would stop the turn before Jev.
        send(runtime, adapter, "hi, I'm Ananya, my battery won't charge, my number is 9876543210")
        dumped = json.dumps(jev.calls[0]["state"])
        self.assertNotIn("9876543210", dumped)
        self.assertNotIn("Ananya", dumped)


if __name__ == "__main__":
    unittest.main()
