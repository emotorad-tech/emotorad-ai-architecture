"""Whole journeys through the assembled runtime.

The unit tests prove each block works. These prove the *order* is right — that
identity runs before the prompt, guardrails before the model, and the post-checks
after it. Every one of these is a conversation a real customer could have.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import AmiigoAdapter, VoiceAdapter, WhatsAppAdapter
from emotorad_ai.config import Settings
from emotorad_ai.disclosure import has_disclosure
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import SEARCH_BATTERY_KNOWLEDGE, build_registry

TODAY = date(2026, 8, 4)


def make_runtime(script, oms_available=True):
    registry = build_registry(today=TODAY, oms_available=oms_available)
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry,
        llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
    )
    return runtime, llm


def whatsapp(runtime, sender, text, conversation_id="c1", **extra):
    adapter = WhatsAppAdapter(runtime.resolver)
    event = {"from": sender, "text": text, "conversation_id": conversation_id}
    event.update(extra)
    return runtime.handle(adapter.to_message(event))


class SingleBikeJourneyTests(unittest.TestCase):
    def test_a_verified_customer_gets_help_without_being_asked_who_they_are(self):
        runtime, llm = make_runtime(
            [
                call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "will not charge"}, "t1"),
                say("Try a different wall socket and check the key is turned on."),
            ]
        )
        reply = whatsapp(runtime, "919876543210", "my battery won't charge")

        self.assertEqual(reply.handled_by, "battery_support")
        self.assertFalse(reply.escalated)
        # The prompt already carried her bike and coverage, so the agent never
        # had to ask what she owns.
        prompt = llm.requests[0]["system"]
        self.assertIn("Ananya Rao", prompt)
        self.assertIn("EMXP2025004417", prompt)

    def test_the_first_reply_of_every_conversation_says_it_is_a_bot(self):
        runtime, _ = make_runtime([say("Try a different socket.")])
        reply = whatsapp(runtime, "919876543210", "battery won't charge")
        self.assertTrue(has_disclosure(reply.text))

    def test_the_disclosure_is_not_repeated_on_later_turns(self):
        runtime, _ = make_runtime([say("First answer."), say("Second answer.")])
        whatsapp(runtime, "919876543210", "battery won't charge")
        second = whatsapp(runtime, "919876543210", "still not working")
        self.assertFalse(has_disclosure(second.text))


class MultiBikeJourneyTests(unittest.TestCase):
    def test_three_bikes_are_disambiguated_before_the_model_is_ever_called(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "my battery won't charge")

        self.assertEqual(llm.requests, [], "no model call to ask which bike")
        self.assertEqual(reply.handled_by, "triage")
        self.assertIn("Which one", reply.text)
        self.assertIn("T-Rex Air", reply.text)

    def test_the_issue_survives_the_selection_turn(self):
        runtime, llm = make_runtime(
            [call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "charging"}, "t1"), say("Try this.")]
        )
        whatsapp(runtime, "919700000001", "my battery won't charge")
        reply = whatsapp(runtime, "919700000001", "the EMX Plus")

        self.assertEqual(reply.handled_by, "battery_support")
        state = runtime.conversations.get("c1")
        self.assertEqual(state.selected_frame, "EMXP2025004990")

    def test_an_unclear_selection_re_asks_rather_than_picking_one(self):
        runtime, llm = make_runtime([])
        whatsapp(runtime, "919700000001", "battery issue")
        reply = whatsapp(runtime, "919700000001", "what are your timings")

        self.assertEqual(llm.requests, [])
        self.assertIn("did not catch", reply.text)
        self.assertIsNone(runtime.conversations.get("c1").selected_frame)


class GuardrailJourneyTests(unittest.TestCase):
    def test_a_safety_report_never_reaches_the_model(self):
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919876543210", "my battery is swelling and smells of burning")

        self.assertEqual(llm.requests, [], "the strongest property in the suite")
        self.assertTrue(reply.escalated)
        self.assertTrue(reply.ticket_id)
        self.assertTrue(has_disclosure(reply.text), "even a short-circuit discloses")

    def test_safety_on_a_multi_bike_number_still_raises_a_ticket(self):
        # The safety branch fires before triage, so no bike has been chosen yet.
        # Refusing to raise the ticket for want of a frame number would be the
        # worst possible time to be pedantic.
        runtime, llm = make_runtime([])
        reply = whatsapp(runtime, "919700000001", "there is smoke coming from the battery")
        self.assertEqual(llm.requests, [])
        self.assertTrue(reply.escalated)

    def test_asking_for_a_human_exits_from_any_phase(self):
        runtime, llm = make_runtime([])
        whatsapp(runtime, "919700000001", "battery issue")   # mid bike-selection
        reply = whatsapp(runtime, "919700000001", "just connect me to an agent")

        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "guardrail:human_handoff")

    def test_a_false_coverage_claim_is_blocked_after_the_model_produces_it(self):
        # Rohit's bike is out of warranty. The model says otherwise — which is
        # exactly the Air Canada shape, and nothing before this point catches it.
        runtime, llm = make_runtime(
            [
                call_tool("lookup_warranty_record", {}, "t1"),
                say("Good news, your battery is still covered under warranty."),
            ]
        )
        reply = whatsapp(runtime, "919812345678", "is my battery covered")

        self.assertEqual(reply.handled_by, "guardrail:coverage_post_check")
        self.assertTrue(reply.escalated)
        self.assertNotIn("covered under warranty", reply.text)
        self.assertEqual(
            reply.metadata["blocked_reason"], "coverage_claim_contradicts_tool_result"
        )

    def test_a_true_coverage_claim_passes_through(self):
        runtime, llm = make_runtime(
            [
                call_tool("lookup_warranty_record", {}, "t1"),
                say("Yes, that is covered under warranty."),
            ]
        )
        reply = whatsapp(runtime, "919876543210", "is my battery covered")
        self.assertEqual(reply.handled_by, "battery_support")


class RegistrationJourneyTests(unittest.TestCase):
    def test_an_unregistered_customer_reaches_registration_not_a_dead_end(self):
        runtime, llm = make_runtime([say("I can help you register it. What is the frame number?")])
        reply = whatsapp(
            runtime, fixtures.PHONE_WITH_NO_RECORD.lstrip("+"), "my battery won't charge"
        )

        self.assertEqual(reply.handled_by, "late_warranty_registration")
        prompt = llm.requests[0]["system"]
        self.assertIn("no bike is registered", prompt)

    def test_a_missing_purchase_date_is_handled_by_battery_support_not_registration(self):
        # The bike IS registered — only the date is missing. Sending them to
        # "register your bike" reads as though we lost their record.
        runtime, llm = make_runtime(
            [call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "charging"}, "t1"), say("Try this.")]
        )
        reply = whatsapp(runtime, "919700000002", "battery won't charge")

        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIn("Coverage: UNKNOWN", llm.requests[0]["system"])


class OutageJourneyTests(unittest.TestCase):
    def test_an_oms_outage_is_not_reported_as_the_customer_being_unregistered(self):
        runtime, llm = make_runtime([say("Let me help with general advice.")], oms_available=False)
        reply = whatsapp(runtime, "919876543210", "battery won't charge")

        prompt = llm.requests[0]["system"]
        self.assertIn("not responding", prompt)
        self.assertNotEqual(reply.handled_by, "late_warranty_registration")


class ChannelParityTests(unittest.TestCase):
    def test_voice_personalises_but_never_discloses_personal_facts(self):
        runtime, llm = make_runtime([say("Let me help with that.")])
        adapter = VoiceAdapter(runtime.resolver)
        reply = runtime.handle(
            adapter.to_message(
                {"caller_id": "+919876543210", "transcript": "my battery won't charge",
                 "call_id": "CALL-1", "confidence": 0.9}
            )
        )
        prompt = llm.requests[0]["system"]
        self.assertNotIn("Ananya", prompt, "caller ID is spoofable")
        self.assertTrue(has_disclosure(reply.text))

    def test_amiigo_and_whatsapp_reach_the_same_person(self):
        runtime, _ = make_runtime([say("a"), say("b")])
        app = AmiigoAdapter(runtime.resolver).to_message(
            {"session_token": "sess-ananya", "text": "hi", "conversation_id": "c-app"}
        )
        chat = WhatsAppAdapter(runtime.resolver).to_message(
            {"from": "919876543210", "text": "hi", "conversation_id": "c-wa"}
        )
        self.assertEqual(app.identity.cluster_id, chat.identity.cluster_id)


class ObservabilityTests(unittest.TestCase):
    def test_the_disclosure_audit_trail_records_what_authorised_it(self):
        runtime, _ = make_runtime([say("Try a different socket.")])
        whatsapp(runtime, "919876543210", "battery won't charge")

        resolved = [e for e in runtime.log.events if e["event"] == "identity_resolved"][0]
        self.assertEqual(resolved["strength"], "verified")
        self.assertTrue(resolved["cluster_id"])

    def test_a_blocked_coverage_claim_is_logged_with_the_suppressed_text(self):
        runtime, _ = make_runtime(
            [call_tool("lookup_warranty_record", {}, "t1"), say("This is covered under warranty.")]
        )
        reply = whatsapp(runtime, "919812345678", "is my battery still covered")

        guardrails = [e for e in runtime.log.events if e["event"] == "guardrail_triggered"]
        self.assertTrue(any(g["guardrail"] == "coverage_post_check" for g in guardrails))
        # What the model tried to say is kept, or nobody can tune the guardrail.
        self.assertIn("covered under warranty", reply.metadata["suppressed_text"])


if __name__ == "__main__":
    unittest.main()
