"""Bot 2 — and the architecture's own test.

If R0 was built right, a second sub-agent is almost entirely configuration. The
tests here assert exactly that: motor support inherits identity, enrichment,
triage, the safety branch, the coverage post-check, disclosure and idempotency
without any of them being reimplemented.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WhatsAppAdapter
from emotorad_ai.agents import motor_support
from emotorad_ai.agents.battery_support import DEFINITION as BATTERY_DEFINITION
from emotorad_ai.config import Settings
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.guardrails import check_battery_safety, check_safety
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 6)


def make_runtime(script):
    registry = build_registry(today=TODAY)
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry,
        llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
    )
    return runtime, llm


def whatsapp(runtime, sender, text, conversation_id="c1"):
    adapter = WhatsAppAdapter(runtime.resolver)
    return runtime.handle(
        adapter.to_message({"from": sender, "text": text, "conversation_id": conversation_id})
    )


class RoutingTests(unittest.TestCase):
    def test_a_motor_complaint_reaches_the_motor_agent(self):
        runtime, llm = make_runtime(
            [call_tool(SEARCH_KNOWLEDGE, {"query": "grinding noise", "topic": "motor"}, "t1"),
             say("Tell me when the noise happens — under power, or while pedalling?")]
        )
        reply = whatsapp(runtime, "919876543210", "the motor is making a grinding noise")
        self.assertEqual(reply.handled_by, "motor_support")

    def test_a_battery_complaint_still_reaches_the_battery_agent(self):
        runtime, llm = make_runtime(
            [call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"), say("Try another socket.")]
        )
        reply = whatsapp(runtime, "919876543210", "battery won't charge")
        self.assertEqual(reply.handled_by, "battery_support")

    def test_a_message_about_both_asks_rather_than_guessing(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "the motor is noisy and the battery drains fast")
        self.assertEqual(llm.requests, [], "ambiguous topic must not reach a model")
        self.assertEqual(reply.handled_by, "triage")

    def test_hindi_motor_complaints_route_correctly(self):
        runtime, llm = make_runtime([say("Let me help.")])
        reply = whatsapp(runtime, "919876543210", "मोटर से आवाज आ रही है")
        self.assertEqual(reply.handled_by, "motor_support")


class InheritedBehaviourTests(unittest.TestCase):
    """Everything R0 built, working for Bot 2 without being rewritten."""

    def test_it_inherits_customer_context_verbatim(self):
        runtime, llm = make_runtime([say("Let me help.")])
        whatsapp(runtime, "919876543210", "motor noise")
        prompt = llm.requests[0]["system"]
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)
        self.assertIn("in warranty", prompt)

    def test_it_inherits_multi_bike_disambiguation(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "motor is noisy")
        self.assertEqual(llm.requests, [])
        self.assertIn("Which one", reply.text)

    def test_it_inherits_the_ai_disclosure(self):
        runtime, _ = make_runtime([say("Let me help.")])
        reply = whatsapp(runtime, "919876543210", "motor noise")
        self.assertTrue(has_disclosure(reply.text))

    def test_it_inherits_the_coverage_post_check(self):
        # The Air Canada guard is persona-level, not battery-specific. Rohit's
        # bike is out of warranty.
        runtime, _ = make_runtime(
            [call_tool("lookup_warranty_record", {}, "t1"),
             say("Your motor is still covered under warranty, so no charge.")]
        )
        reply = whatsapp(runtime, "919812345678", "motor making noise, is it covered")
        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertTrue(reply.escalated)

    def test_it_inherits_the_human_handoff(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "motor noise, connect me to an agent")
        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")

    def test_the_context_blocks_are_shared_not_copied(self):
        # A second copy would drift, and the copy that drifts is the one that
        # starts stating coverage it should not.
        self.assertIs(
            motor_support.build_system_prompt.__globals__["_facts_block"],
            BATTERY_DEFINITION.build_system_prompt.__globals__["_facts_block"],
        )


class MotorSafetyTests(unittest.TestCase):
    """A drive fault risks a bike failing while someone rides it."""

    def test_loss_of_control_reports_never_reach_the_model(self):
        for text in (
            "my brakes are not working",
            "the rear wheel locked while riding",
            "the motor started on its own",
            "I fell off when the power cut",
        ):
            runtime, llm = make_runtime([])
            reply = whatsapp(runtime, "919876543210", text, conversation_id="c-%s" % hash(text))
            self.assertEqual(llm.requests, [], text)
            self.assertTrue(reply.escalated, text)

    def test_any_report_of_injury_escalates_whatever_the_component(self):
        self.assertTrue(check_safety("I hurt myself when it happened").triggered)
        self.assertTrue(check_safety("there was an accident").triggered)

    def test_routine_power_cuts_are_still_ordinary_support(self):
        # This is the common intermittent-connector complaint and has its own
        # knowledge record. Treating it as a safety stop would break the happy
        # path for every customer with a loose battery latch.
        runtime, llm = make_runtime(
            [call_tool(SEARCH_KNOWLEDGE, {"query": "cuts out", "topic": "motor"}, "t1"),
             say("Check the battery is latched firmly.")]
        )
        reply = whatsapp(runtime, "919876543210", "power cuts out sometimes while riding")
        self.assertEqual(reply.handled_by, "motor_support")
        self.assertFalse(reply.escalated)

    def test_the_safety_gate_runs_before_triage_knows_the_topic(self):
        # Safety fires before the topic is known, so it cannot depend on having
        # been routed to the right agent first.
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "my brakes are not working")
        self.assertEqual(llm.requests, [])
        self.assertTrue(reply.escalated)

    def test_battery_only_scan_still_exists_for_callers_that_want_it(self):
        self.assertTrue(check_battery_safety("the battery is swelling").triggered)
        self.assertFalse(check_battery_safety("my brakes are not working").triggered)


class ToolSliceTests(unittest.TestCase):
    def test_the_motor_agent_gets_no_battery_diagnostics_tool(self):
        self.assertNotIn("get_battery_diagnostics", motor_support.TOOL_NAMES)

    def test_it_can_still_look_up_warranty_and_raise_tickets(self):
        for tool in ("lookup_warranty_record", "create_support_ticket", SEARCH_KNOWLEDGE):
            self.assertIn(tool, motor_support.TOOL_NAMES, tool)


if __name__ == "__main__":
    unittest.main()
