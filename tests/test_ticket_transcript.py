import unittest

from emotorad_ai.conversation import InMemoryConversationStore, TranscriptTurn, render_transcript
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
        runtime = runtime_on(InMemoryConversationStore(), [
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

    def test_a_ticket_raised_in_a_turn_the_evidence_check_blocked_is_still_tracked(self):
        # The tool ran, so the ticket exists; blocking the reply must not lose it.
        runtime = runtime_on(InMemoryConversationStore(), [
            say("Is the charger light on?"),
            call_tool(CREATE_SUPPORT_TICKET, {"category": "battery_charging", "severity": "normal",
                                              "description": "LED stays off.", "idempotency_key": "k1"}, "toolu_1"),
            say("I have raised a ticket."),
        ])
        send(runtime, "my battery won't charge")
        reply = send(runtime, "no, the light is off")
        self.assertEqual(reply.handled_by, "guardrail:evidence_post_check")
        self.assertEqual(reply.ticket_id, "EM-00001")
        self.assertIn("Customer: no, the light is off", runtime.registry.tickets.tickets["EM-00001"]["transcript"])

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
