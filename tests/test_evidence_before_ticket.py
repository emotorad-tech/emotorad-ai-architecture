"""No fault ticket before evidence, enforced where the ticket is created.

Found in the person's combined OpenRouter and MongoDB test on 2026-09-29: with
no photo in the conversation, Haiku raised ticket EM-00001 and said so; the
evidence post-check replaced the reply with "please send a photo", so the
customer never saw the reference; asked again for a complaint, they got the
same reply, a loop. The person's rule (Sachin's, in guardrails.py): no fault
ticket until a photo or video has arrived; safety tickets are exempt.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.base import HANDOVER_TEXT
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.guardrails import COVERAGE_BLOCKED_MESSAGE, EVIDENCE_BLOCKED_MESSAGE
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import BOOK_SERVICE_SLOT, CREATE_SUPPORT_TICKET, FIND_SERVICE_SLOTS, build_registry
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 7, 28)
TICKET = {"category": "battery_charging", "severity": "normal", "description": "Tried four steps; still dead.",
          "idempotency_key": "k1"}


def context(evidence=None):
    late = {} if evidence is None else {"evidence_seen": lambda: evidence}
    return ToolContext(conversation_id="c1", phone="+919876543210", late=late)


class TicketToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)

    def test_a_fault_ticket_is_refused_until_evidence_has_arrived(self):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(evidence=False))
        self.assertEqual(envelope["error"]["code"], "evidence_required")
        self.assertEqual(envelope["error"]["remedy"], "collect_evidence")
        self.assertEqual(self.registry.tickets.tickets, {})

    def test_with_evidence_the_ticket_is_raised(self):
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(evidence=True))
        self.assertEqual(envelope["data"]["ticket_id"], "EM-00001")

    def test_a_safety_ticket_never_waits_for_a_photo(self):
        safety = dict(TICKET, category="battery_safety", severity="critical", idempotency_key="safety:c1")
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, safety, context(evidence=False))
        self.assertEqual(envelope["data"]["ticket_id"], "EM-00001")

    def test_a_caller_that_does_not_know_the_evidence_state_is_not_refused(self):
        # The safety branch and the MongoDB smoke test call the tool directly.
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, dict(TICKET), context(evidence=None))
        self.assertEqual(envelope["data"]["ticket_id"], "EM-00001")

    def test_the_model_cannot_claim_evidence_for_itself(self):
        forged = dict(TICKET, evidence_seen=True)
        envelope = self.registry.call(CREATE_SUPPORT_TICKET, forged, context(evidence=False))
        self.assertEqual(envelope["error"]["code"], "evidence_required")
        self.assertNotIn("evidence_seen", self.registry.specs[CREATE_SUPPORT_TICKET].schema()["input_schema"]["properties"])


def routed_store():
    """A conversation already with the battery agent, as the person's was by turn five."""
    store = InMemoryConversationStore()
    store.get("c1").route_to("battery_support")
    return store


def runtime(replies, store=None):
    registry = build_registry(today=TODAY)
    return Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                   log=EventLog(path=None), resolver=IdentityResolver(registry),
                   conversations=store or InMemoryConversationStore())


def send(rt, text, attachments=()):
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message(
        {"conversation_id": "c1", "session_token": "sess-ananya", "text": text, "attachments": list(attachments)}))


class TheCombinedTestSessionTests(unittest.TestCase):
    """The person's session, replayed with scripted models."""

    def test_three_asks_then_a_person_and_no_ticket_created_without_evidence(self):
        # Three asks in all (video-first spec, 2026-10-01), then a person.
        rt = runtime([
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"),
            say("I've raised a support ticket for you; the team will be in touch."),
            say("I have raised a support ticket, as you asked."),
            say("I have raised a support ticket, as you asked."),
            say("I have raised a support ticket, as you asked."),
        ], store=routed_store())
        first = send(rt, "room temperature")
        self.assertEqual(rt.registry.tickets.tickets, {})  # refused at the tool
        self.assertIn(EVIDENCE_BLOCKED_MESSAGE, first.text)
        for text in ("please raise a complaint", "raise it please"):
            self.assertIn(EVIDENCE_BLOCKED_MESSAGE, send(rt, text).text)
        fourth = send(rt, "I said raise it")
        self.assertNotIn(EVIDENCE_BLOCKED_MESSAGE, fourth.text)
        self.assertIn(HANDOVER_TEXT, fourth.text)
        self.assertTrue(fourth.escalated)


class NeverHideAWriteTests(unittest.TestCase):
    """A post-check that replaces a reply still names what the turn did."""

    def test_a_blocked_reply_after_a_booking_names_the_booking(self):
        rt = runtime([
            call_tool(FIND_SERVICE_SLOTS, {"pincode": "411045"}, "toolu_1"),
            call_tool(BOOK_SERVICE_SLOT, {"centre_id": "SC-PUN-01", "slot": "2026-08-01T10:00:00+05:30",
                                          "idempotency_key": "b1"}, "toolu_2"),
            say("Booked. Since your bike is out of warranty, repairs are chargeable."),
        ], store=routed_store())
        answer = send(rt, "book me a service visit, pincode 411045")
        self.assertEqual(answer.handled_by, "guardrail:coverage_post_check")
        self.assertIn(COVERAGE_BLOCKED_MESSAGE, answer.text)
        self.assertIn("BK-00001", answer.text)

    def test_a_blocked_reply_after_a_ticket_names_the_ticket(self):
        photo = {"kind": "image", "url": "data:image/jpeg;base64,/9j/AAAA"}
        rt = runtime([
            call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"),
            say("Good news: this is covered under warranty, so the replacement is free."),
        ])
        answer = send(rt, "here is the charger light, still dead", attachments=[photo])
        self.assertEqual(answer.handled_by, "guardrail:coverage_post_check")
        self.assertEqual(answer.ticket_id, "EM-00001")
        self.assertIn("EM-00001", answer.text)


if __name__ == "__main__":
    unittest.main()
