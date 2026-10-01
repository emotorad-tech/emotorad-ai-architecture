"""The backstop after every reply (spec 2026-10-01, photo safety)."""

import unittest
from unittest import mock

from emotorad_ai.contract import VERIFIED, Identity
from emotorad_ai.llm import call_tool, say
from emotorad_ai.runtime import HANDOVER_TEXT
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET
from tests.test_reply_claims import STAGING_REPLY
from tests.test_verify_first import ONE_BIKE, Chat

RIDER = Identity(strength=VERIFIED, phone=ONE_BIKE, em_aid="aid-1")
TICKET = {"category": "battery_safety", "severity": "critical", "description": "Smoke from the pack.",
          "idempotency_key": "model-1"}


def chat_saying(*replies):
    return Chat(replies=list(replies))


def ask(chat, text="my battery isn't charging", photo=False):
    return chat.say(text, photo=photo, identity=RIDER)


class SafetyBackstopTests(unittest.TestCase):
    def test_a_safety_reply_with_no_ticket_gets_one_on_that_turn(self):
        chat = chat_saying(say(STAGING_REPLY))
        reply = ask(chat)
        tickets = list(chat.registry.tickets.tickets.values())
        self.assertEqual(len(tickets), 1)
        self.assertEqual((tickets[0]["category"], tickets[0]["severity"]), ("battery_safety", "critical"))
        self.assertIn("I have raised this as a priority safety case, reference %s." % reply.ticket_id, reply.text)
        self.assertTrue(reply.escalated)
        self.assertTrue(any(e["event"] == "guardrail_triggered" and e.get("guardrail") == "safety_backstop"
                            for e in chat.log.events))

    def test_a_ticket_raised_in_the_turn_is_not_doubled(self):
        chat = chat_saying(call_tool(CREATE_SUPPORT_TICKET, TICKET, "t1"), say(STAGING_REPLY))
        ask(chat)
        self.assertEqual(len(chat.registry.tickets.tickets), 1)

    def test_a_failing_backstop_leaves_the_reply_and_is_logged(self):
        chat = chat_saying(say(STAGING_REPLY))
        with mock.patch.object(chat.runtime, "_raise_safety_ticket", side_effect=RuntimeError("down")):
            reply = ask(chat)
        self.assertEqual(reply.text.count("priority safety case"), 0)
        (event,) = [e for e in chat.log.events if e["event"] == "safety_backstop_failed"]
        self.assertEqual(event["error"], "RuntimeError")


class TicketClaimBackstopTests(unittest.TestCase):
    def test_an_unbacked_claim_is_handed_to_a_person(self):
        # After a photo: without one the evidence check already replaces a
        # ticket claim with a request for a photo.
        chat = chat_saying(say("I've raised a support ticket for your charger; the team will call you."))
        reply = ask(chat, "here is the charger", photo=True)
        self.assertEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")
        self.assertIn(HANDOVER_TEXT, reply.text)  # after the first reply's AI disclosure
        self.assertTrue(reply.escalated)

    def test_an_offer_is_left_alone(self):
        chat = chat_saying(say("Is the charger light on? I can raise a ticket if you'd like."))
        reply = ask(chat)
        self.assertNotEqual(reply.handled_by, "guardrail:ticket_promise_unbacked")

    def test_a_later_mention_of_a_real_ticket_is_left_alone(self):
        chat = chat_saying(call_tool(CREATE_SUPPORT_TICKET, TICKET, "t1"), say("Ticket raised."),
                           say("Your ticket has been raised and the team will call you today."))
        first = ask(chat)
        self.assertTrue(first.ticket_id)
        later = ask(chat, "ok, when will they call?")
        self.assertNotEqual(later.handled_by, "guardrail:ticket_promise_unbacked")
