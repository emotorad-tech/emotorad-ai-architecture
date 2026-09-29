import unittest

from emotorad_ai.conversation import InMemoryConversationStore, TranscriptTurn, render_transcript
from emotorad_ai.guardrails import EVIDENCE_BLOCKED_MESSAGE
from emotorad_ai.llm import call_tool, say
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET
from tests.test_runtime_persistence import runtime_on, send


class RenderTests(unittest.TestCase):
    def test_plain_text_with_times_and_speakers(self):
        text = render_transcript([
            TranscriptTurn(1, "customer", "won't charge", "2026-09-28T09:41:00+00:00"),
            TranscriptTurn(2, "bot", "Try another socket.", "2026-09-28T09:41:05+00:00"),
        ])
        self.assertEqual(text, "[09:41] Customer: won't charge\n[09:41] Bot: Try another socket.")


class TicketTranscriptTests(unittest.TestCase):
    def test_an_agent_ticket_carries_the_thread_including_this_turn(self):
        store = InMemoryConversationStore()
        # The customer sent a photo earlier: a fault ticket needs evidence (test_evidence_before_ticket).
        store.get("conv-1").evidence_seen = True
        runtime = runtime_on(store, [
            say("Is the charger light on?"),
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1"),
            say("Your reference is EM-00001; the team will call you."),
        ])
        send(runtime, "my battery won't charge")
        reply = send(runtime, "no, the light is off")
        transcript = runtime.registry.tickets.tickets[reply.ticket_id]["transcript"]
        self.assertIn("Customer: my battery won't charge", transcript)
        self.assertIn("Customer: no, the light is off", transcript)
        self.assertIn("Bot: Your reference is EM-00001", transcript)

    def test_without_a_photo_no_ticket_is_raised_and_the_reply_asks_for_one(self):
        # This used to pin a ticket raised with no evidence and then hidden by
        # the evidence check. The rule is now enforced at the tool as well
        # (the person's decision, 2026-09-29), so no such ticket exists; a
        # blocked reply that follows a real write still names it
        # (test_evidence_before_ticket.NeverHideAWriteTests).
        runtime = runtime_on(InMemoryConversationStore(), [
            say("Is the charger light on?"),
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1"),
            say("I have raised a ticket."),
        ])
        send(runtime, "my battery won't charge")
        reply = send(runtime, "no, the light is off")
        self.assertEqual(reply.handled_by, "guardrail:evidence_post_check")
        self.assertIsNone(reply.ticket_id)
        self.assertEqual(runtime.registry.tickets.tickets, {})
        self.assertIn(EVIDENCE_BLOCKED_MESSAGE, reply.text)

    def test_a_safety_ticket_carries_it_too(self):
        runtime = runtime_on(InMemoryConversationStore(), [])
        reply = send(runtime, "my battery is swollen")
        self.assertIn("Customer: my battery is swollen", runtime.registry.tickets.tickets[reply.ticket_id]["transcript"])

    def test_a_failed_attachment_is_logged_and_the_reply_still_goes_out(self):
        runtime = runtime_on(InMemoryConversationStore(), [])
        runtime.registry.tickets.attach_transcript = lambda *a: (_ for _ in ()).throw(RuntimeError("zoho down"))
        reply = send(runtime, "my battery is swollen")
        self.assertTrue(reply.ticket_id)
        self.assertTrue(any(e["event"] == "transcript_attach_failed" for e in runtime.log.events))


if __name__ == "__main__":
    unittest.main()
