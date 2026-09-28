"""Regression tests for the final-review findings on feat/jev-routing.

Each test reproduces one finding and fails without its fix.
"""

import http.client
import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter, WhatsAppAdapter
from emotorad_ai.config import Settings
from emotorad_ai.decisions import (
    NONE,
    NONE_OF_THESE,
    Q_CATEGORY,
    Q_SUB_CATEGORY,
    Thresholds,
    build_catalogue,
    build_state,
    route,
)
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, JevError, ScriptedJev, choice, choose, parse_decision
from emotorad_ai.knowledge import KnowledgeBase
from emotorad_ai.llm import ScriptedClaude, call_tool, from_openai_response, say
from emotorad_ai.metrics import build_report
from emotorad_ai.observability import EventLog
from emotorad_ai.openrouter import OpenRouterBadResponse, OpenRouterTransport, OpenRouterUnavailable
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, MockTicketSystem, build_registry

TODAY = date(2026, 7, 28)
NARROW = {Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)}
UNSURE = {Q_CATEGORY: choose("battery", 0.4), Q_SUB_CATEGORY: choose("none", 0.5)}


class ThenRaise:
    """Replays queued responses, then raises an OpenRouter outage."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def create(self, system, messages, tools):
        self.requests.append({"system": system, "messages": list(messages)})
        if self.responses:
            return self.responses.pop(0)
        raise OpenRouterUnavailable("down")


def build(jev_decisions, fallback_llm=None, narrow_llm=None, tickets=None):
    registry = build_registry(today=TODAY, ticket_system=tickets)
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=fallback_llm or ScriptedClaude([]),
        narrow_llm=narrow_llm or ScriptedClaude([]),
        jev=ScriptedJev(jev_decisions),
        standard_responses=[],
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    return runtime


def web(runtime, text, session="sess-ananya"):
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"conversation_id": "conv-1", "session_token": session, "text": text}))


# -- Important 1: untyped errors must become typed ones -----------------------

class UntypedErrorTests(unittest.TestCase):
    def test_a_jev_choice_of_the_wrong_json_type_is_a_bad_response(self):
        questions = {"category": choice("x", {"battery": "b", "motor": "m"})}
        payload = {"answers": {"category": {"type": "choice", "choice": {"label": "battery"},
                                            "probabilities": {"battery": 0.9}, "confidence": 0.9}}}
        with self.assertRaises(JevError) as caught:
            parse_decision(payload, questions)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_tool_arguments_sent_as_an_object_are_accepted(self):
        body = {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "find_service_slots", "arguments": {"pincode": "411001"}}}
        ]}}]}
        self.assertEqual(from_openai_response(body).tool_uses[0].arguments, {"pincode": "411001"})

    def test_a_malformed_choice_entry_is_a_bad_response(self):
        with self.assertRaises(OpenRouterBadResponse):
            from_openai_response({"choices": ["not a dict"]})

    def test_a_connection_dropped_mid_body_is_unavailable(self):
        class Dropped:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"{")

        transport = OpenRouterTransport(api_key="k", base_url="http://127.0.0.1:9", opener=lambda *a, **k: Dropped())
        with self.assertRaises(OpenRouterUnavailable):
            transport.post("/x", {})


# -- Important 2: a narrow failure after a write must not repeat it -----------

class NarrowWriteThenFailTests(unittest.TestCase):
    def test_a_ticket_raised_before_the_narrow_model_fails_is_not_raised_again(self):
        tickets = MockTicketSystem()
        narrow = ThenRaise([call_tool(CREATE_SUPPORT_TICKET, {
            "category": "battery_charging", "severity": "normal",
            "description": "Charger LED stays off.", "idempotency_key": "k1",
        }, "toolu_1")])
        fallback = ScriptedClaude([say("I have raised a ticket.")])
        runtime = build([JevDecision(answers=NARROW)], fallback_llm=fallback, narrow_llm=narrow, tickets=tickets)
        reply = web(runtime, "my battery won't charge")
        self.assertEqual(list(tickets.tickets), ["EM-00001"])
        self.assertEqual(fallback.requests, [])
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.ticket_id, "EM-00001")
        self.assertIn("EM-00001", reply.text)


# -- Important 3: every agent node hands over on an OpenRouter failure -------

class EveryAgentHandsOverTests(unittest.TestCase):
    def test_the_late_warranty_agent_hands_over_instead_of_crashing(self):
        runtime = build([], fallback_llm=ThenRaise())
        message = WhatsAppAdapter(runtime.resolver).to_message({"from": "919700000009", "text": "hi"})
        reply = runtime.handle(message)
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.handled_by, "llm_error")


# -- Important 4: rule 4 must not continue past a confident different answer -

class Rule4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogue = build_catalogue(KnowledgeBase())

    def go(self, answers, bike=None):
        return route(JevDecision(answers=answers), None, Thresholds(), self.catalogue,
                     current_sub_category="battery-wont-charge", bike=bike or {"product_name": "EMX Plus"})

    def test_a_confident_none_of_these_mid_flow_goes_to_the_full_agent(self):
        result = self.go({Q_CATEGORY: choose(NONE_OF_THESE, 0.99), Q_SUB_CATEGORY: choose(NONE, 0.99)})
        self.assertEqual(result.path, "full")

    def test_a_confident_different_record_that_does_not_apply_does_not_continue(self):
        result = self.go({Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-power-on", 0.95)},
                         bike={"product_name": "Doodle V3"})
        self.assertEqual(result.path, "full")

    def test_a_confident_record_from_the_other_topic_does_not_continue(self):
        result = self.go({Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("motor-noise", 0.95)})
        self.assertEqual(result.path, "full")

    def test_a_confident_none_sub_category_on_the_same_topic_still_continues(self):
        result = self.go({Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose(NONE, 0.9)})
        self.assertEqual((result.path, result.sub_category), ("narrow", "battery-wont-charge"))


# -- Important 5: metrics count the path that answered ------------------------

class FinalPathMetricTests(unittest.TestCase):
    def test_a_narrow_turn_that_fell_back_is_counted_as_full_and_jev_errors_are_counted(self):
        runtime = build(
            [JevDecision(answers=NARROW), JevError("rate_limited", "slow down")],
            fallback_llm=ScriptedClaude([say("Tell me more."), say("Anything else?")]),
            narrow_llm=ThenRaise(),
        )
        web(runtime, "my battery won't charge")
        web(runtime, "still not charging")
        report = build_report(runtime.log.events)
        self.assertEqual(report.by_path, {"full": 2})
        self.assertEqual(report.jev_errors, {"rate_limited": 1})


# -- Important (re-graded minor 9): Jev's state carries no phone or warranty --

class JevStatePrivacyTests(unittest.TestCase):
    def test_a_spaced_phone_and_assistant_warranty_text_never_reach_jev(self):
        history = [
            {"role": "user", "content": "my battery won't charge"},
            {"role": "assistant", "content": [{"type": "text", "text": "Your EMX Plus is in warranty until 12 March 2027."}]},
        ]
        state = build_state("call back on 98765 43210 or +91-98765-43210", history, "whatsapp",
                            redact=("+919876543210",))
        dumped = json.dumps(state)
        self.assertNotIn("98765", dumped)
        self.assertNotIn("2027", dumped)
        self.assertEqual(state["recent_turns"], ["user: my battery won't charge"])

    def test_name_redaction_matches_whole_words_only(self):
        state = build_state("Ram here, the program on the display is stuck", [], "whatsapp", redact=("Ram",))
        self.assertIn("program", state["message"])
        self.assertNotIn("Ram ", state["message"])


if __name__ == "__main__":
    unittest.main()
