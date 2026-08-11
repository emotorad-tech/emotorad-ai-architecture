import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import Agent
from emotorad_ai.agents.battery_support import AGENT_NAME, DEFINITION
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog, redact_pii
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, SEARCH_BATTERY_KNOWLEDGE, build_registry

TODAY = date(2026, 7, 28)


def make_runtime(responses):
    registry = build_registry(today=TODAY)
    llm = ScriptedClaude(responses)
    runtime = Runtime(
        settings=Settings(log_path="", log_to_stdout=False),
        registry=registry,
        llm=llm,
        log=EventLog(path=None),
        resolver=IdentityResolver(registry),
    )
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime, adapter, llm


def send(runtime, adapter, text, session="sess-ananya", conversation_id="conv-1", pill=None):
    message = adapter.to_message(
        {"conversation_id": conversation_id, "session_token": session, "text": text, "pill": pill}
    )
    return runtime.handle(message)


class AgentLoopTests(unittest.TestCase):
    def test_runs_tools_then_answers(self):
        registry = build_registry(today=TODAY)
        llm = ScriptedClaude(
            [
                call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "will not charge"}, "toolu_1"),
                call_tool(
                    CREATE_SUPPORT_TICKET,
                    {
                        "category": "battery_charging",
                        "description": "Charger LED stays off; tried another socket.",
                        "severity": "normal",
                        "idempotency_key": "conv-1-ticket",
                    },
                    "toolu_2",
                ),
                say("I have raised ticket EM-00001 for you."),
            ]
        )
        agent = Agent(DEFINITION, registry, llm, EventLog(path=None), Settings(log_path=""))
        adapter = WebsiteChatAdapter(IdentityResolver(registry))
        message = adapter.to_message({"session_token": "sess-ananya", "text": "battery won't charge"})
        resolved = adapter.resolver.hydrate(message)

        turn = agent.run(message, resolved, [])

        self.assertEqual(turn.ticket_id, "EM-00001")
        self.assertEqual([c["tool"] for c in turn.tool_calls], [SEARCH_BATTERY_KNOWLEDGE, CREATE_SUPPORT_TICKET])
        self.assertEqual(turn.iterations, 3)
        self.assertFalse(turn.escalate)

    def test_system_prompt_states_ownership_and_coverage_as_fact(self):
        registry = build_registry(today=TODAY)
        adapter = WebsiteChatAdapter(IdentityResolver(registry))

        in_warranty = adapter.to_message({"session_token": "sess-ananya", "text": "hi"})
        prompt = DEFINITION.build_system_prompt(in_warranty, adapter.resolver.hydrate(in_warranty))
        self.assertIn("EMX Plus", prompt)
        self.assertIn("in warranty", prompt)

        out_of_warranty = adapter.to_message({"session_token": "sess-rohit", "text": "hi"})
        prompt = DEFINITION.build_system_prompt(out_of_warranty, adapter.resolver.hydrate(out_of_warranty))
        self.assertIn("out of warranty", prompt)

    def test_missing_idempotency_key_is_backfilled_rather_than_failing_the_ticket(self):
        registry = build_registry(today=TODAY)
        llm = ScriptedClaude(
            [
                call_tool(
                    CREATE_SUPPORT_TICKET,
                    {"category": "battery_range", "description": "range dropped", "severity": "normal"},
                    "toolu_1",
                ),
                say("Ticket raised."),
            ]
        )
        agent = Agent(DEFINITION, registry, llm, EventLog(path=None), Settings(log_path=""))
        adapter = WebsiteChatAdapter(IdentityResolver(registry))
        message = adapter.to_message({"session_token": "sess-ananya", "text": "range dropped"})

        turn = agent.run(message, adapter.resolver.hydrate(message), [])

        self.assertIsNotNone(turn.ticket_id)
        self.assertTrue(turn.tool_calls[0]["arguments"]["idempotency_key"])

    def test_runaway_tool_loop_hands_over_instead_of_spinning(self):
        registry = build_registry(today=TODAY)
        settings = Settings(log_path="", max_agent_iterations=2)
        llm = ScriptedClaude(
            [
                call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "a"}, "toolu_1"),
                call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "b"}, "toolu_2"),
            ]
        )
        agent = Agent(DEFINITION, registry, llm, EventLog(path=None), settings)
        adapter = WebsiteChatAdapter(IdentityResolver(registry))
        message = adapter.to_message({"session_token": "sess-ananya", "text": "battery issue"})

        turn = agent.run(message, adapter.resolver.hydrate(message), [])

        self.assertTrue(turn.escalate)
        self.assertEqual(turn.iterations, 2)


class RuntimeTests(unittest.TestCase):
    def test_safety_report_never_reaches_the_model(self):
        runtime, adapter, llm = make_runtime([])  # any model call would raise

        reply = send(runtime, adapter, "the battery is swelling and smells like burning")

        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:battery_safety")
        self.assertTrue(reply.escalated)
        self.assertIsNotNone(reply.ticket_id)
        self.assertIn("stop using and stop charging", reply.text)

        ticket = runtime.registry.tickets.tickets[reply.ticket_id]
        self.assertEqual(ticket["severity"], "critical")
        self.assertEqual(ticket["category"], "battery_safety")

    def test_repeated_safety_message_does_not_open_a_second_case(self):
        runtime, adapter, _ = make_runtime([])
        first = send(runtime, adapter, "battery is swelling")
        second = send(runtime, adapter, "battery is swelling")
        self.assertEqual(first.ticket_id, second.ticket_id)
        self.assertEqual(len(runtime.registry.tickets.tickets), 1)

    def test_request_for_a_human_exits_immediately(self):
        runtime, adapter, llm = make_runtime([])

        reply = send(runtime, adapter, "this isn't working, let me talk to a human")

        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")
        self.assertTrue(reply.escalated)

    def test_ordinary_complaint_routes_to_the_battery_agent(self):
        runtime, adapter, llm = make_runtime(
            [
                call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "will not charge"}, "toolu_1"),
                say("Try a different socket and make sure the key is turned on."),
            ]
        )

        reply = send(runtime, adapter, "my battery won't charge", pill="battery_issue")

        self.assertEqual(reply.handled_by, AGENT_NAME)
        self.assertFalse(reply.escalated)
        self.assertEqual(reply.metadata["tool_calls"], [SEARCH_BATTERY_KNOWLEDGE])
        routed = [e for e in runtime.log.events if e["event"] == "routed"]
        # The reason records how we knew, not just what we decided: a mistake
        # from a tapped pill is a different bug from one from free text.
        self.assertEqual(routed[0]["reason"], "pill:battery_issue->battery")

    def test_unknown_visitor_is_not_handed_to_the_customer_agent(self):
        runtime, adapter, llm = make_runtime([])

        reply = send(runtime, adapter, "hello", session="sess-unknown")

        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "router")
        self.assertTrue(reply.escalated)

    def test_every_step_of_the_turn_is_logged(self):
        runtime, adapter, _ = make_runtime(
            [call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "x"}, "toolu_1"), say("done")]
        )
        send(runtime, adapter, "my battery won't charge")

        events = [e["event"] for e in runtime.log.events]
        for expected in ["inbound", "identity_resolved", "routed", "llm_turn", "tool_call", "outcome"]:
            self.assertIn(expected, events)

    def test_conversation_history_survives_across_turns(self):
        runtime, adapter, _ = make_runtime([say("first"), say("second")])
        send(runtime, adapter, "hello")
        send(runtime, adapter, "still there?")
        history = runtime.conversations.history("conv-1")
        self.assertEqual(len(history), 4)  # user, assistant, user, assistant


class RedactionTests(unittest.TestCase):
    def test_contact_details_are_stripped_from_logged_text(self):
        redacted = redact_pii("call me on 9876543210 or ananya@example.com")
        self.assertNotIn("9876543210", redacted)
        self.assertNotIn("ananya@example.com", redacted)


if __name__ == "__main__":
    unittest.main()
