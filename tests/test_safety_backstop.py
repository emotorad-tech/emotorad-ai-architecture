"""The backstop for ticket claims (spec 2026-10-01, photo safety, revised).

It acts only on a claim that a ticket exists when none does. Advice is never
acted on."""

import unittest
from unittest import mock

from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity
from emotorad_ai.guardrails import SAFETY_NOT_RECORDED_MESSAGE
from emotorad_ai.llm import call_tool, say
from emotorad_ai.runtime import HANDOVER_TEXT
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, RAISE_INTAKE_TICKET
from tests.test_reply_claims import STAGING_REPLY
from tests.test_verify_first import ONE_BIKE, Chat

RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
TICKET = {"category": "battery_safety", "severity": "critical", "description": "Smoke from the pack.",
          "idempotency_key": "model-1"}


def ask(chat, text="my battery isn't charging", photo=False, identity=RIDER):
    return chat.say(text, photo=photo, identity=identity)


def tickets(chat):
    return list(chat.registry.tickets.tickets.values())


class HazardClaimTests(unittest.TestCase):
    def test_an_unbacked_claim_about_a_live_hazard_raises_the_safety_ticket(self):
        chat = Chat(replies=[say(STAGING_REPLY)])
        reply = ask(chat)
        (ticket,) = tickets(chat)
        self.assertEqual((ticket["category"], ticket["severity"]), ("battery_safety", "critical"))
        self.assertIn("I have raised this as a priority safety case, reference %s." % reply.ticket_id, reply.text)
        self.assertTrue(reply.escalated)
        self.assertTrue(any(e["event"] == "guardrail_triggered" and e.get("guardrail") == "safety_backstop"
                            for e in chat.log.events))

    def test_advice_without_a_claim_never_raises_a_ticket(self):
        advice = ("The pack looks healthy. Stop charging at 80% to keep it that way. "
                  "If you ever see smoke, stop charging it and move it outside.")
        chat = Chat(replies=[say(advice)])
        reply = ask(chat, photo=True)
        self.assertEqual(tickets(chat), [])
        self.assertEqual(reply.handled_by, "battery_support")

    def test_a_hazard_claim_with_no_known_customer_gets_the_steps_and_no_promise(self):
        # Spec 2026-10-05, section 6, and the plan's cross-check, finding 13:
        # with Zoho off nothing can be recorded for a visitor with no number,
        # so neither a ticket nor a hand-over is promised. With Zoho on, the
        # number is asked for (tests/test_safety_without_phone.py).
        chat = Chat(replies=[say(STAGING_REPLY)], verify_first=False)
        reply = ask(chat, identity=Identity(strength=ANONYMOUS, em_aid="aid-1"))
        self.assertEqual(tickets(chat), [])
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIn(SAFETY_NOT_RECORDED_MESSAGE, reply.text)
        self.assertNotIn("I'm raising", reply.text)
        self.assertNotIn(HANDOVER_TEXT, reply.text)
        self.assertFalse(reply.escalated)
        self.assertEqual(len(chat.llm.requests), 1)

    def test_a_hazard_claim_whose_ticket_cannot_be_raised_is_handed_over(self):
        chat = Chat(replies=[say(STAGING_REPLY)])
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", return_value=None):
            reply = ask(chat)
        self.assertEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")
        self.assertIn(HANDOVER_TEXT, reply.text)


class OtherClaimTests(unittest.TestCase):
    def test_an_unbacked_claim_is_handed_to_a_person(self):
        # After a photo: without one the evidence check already replaces a
        # ticket claim with a request for a photo.
        chat = Chat(replies=[say("I've raised a support ticket for your charger; the team will call you.")])
        reply = ask(chat, "here is the charger", photo=True)
        self.assertEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")
        self.assertIn(HANDOVER_TEXT, reply.text)  # after the first reply's AI disclosure
        self.assertTrue(reply.escalated)

    def test_an_offer_is_left_alone(self):
        chat = Chat(replies=[say("Is the charger light on? I can raise a ticket if you'd like.")])
        self.assertNotEqual(ask(chat).handled_by, "guardrail:ticket_promise_unbacked")

    def test_a_claim_after_an_intake_ticket_this_turn_is_left_alone(self):
        intake = {"summary": "Charger not working", "idempotency_key": "intake-1"}
        chat = Chat(replies=[call_tool(RAISE_INTAKE_TICKET, intake, "t1"),
                             say("I've raised a ticket and the team will call you.")])
        reply = ask(chat, "here is the charger", photo=True)
        self.assertNotEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")

    def test_a_ticket_the_conversation_holds_is_left_alone_and_never_doubled(self):
        chat = Chat(replies=[call_tool(CREATE_SUPPORT_TICKET, TICKET, "t1"), say("Ticket raised."),
                             say("Your ticket has been raised. Keep it outside; the swollen pack must not be charged.")])
        first = ask(chat)
        self.assertTrue(first.ticket_id)
        later = ask(chat, "ok, when will they call?")
        self.assertNotEqual(later.handled_by, "guardrail:ticket_promise_unbacked")
        self.assertEqual(len(tickets(chat)), 1)

    def test_a_reference_from_the_context_is_left_alone(self):
        chat = Chat(replies=[say("I can see your earlier ticket EM-00007 was raised on 12 September.")])
        state = chat.conversations.get("c1")
        state.context_block = "Earlier conversation: battery charging, ticket EM-00007 (12 September)."
        reply = ask(chat, "here is the charger", photo=True)
        self.assertNotEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")
