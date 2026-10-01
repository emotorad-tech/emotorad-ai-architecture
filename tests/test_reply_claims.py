"""What the backstop treats as a ticket claim (spec 2026-10-01, revised).

Advice is never acted on: the first version also read stop instructions as a
safety reply, and the final review found it raised critical tickets on
ordinary advice ("Stop charging it if you ever see smoke")."""

import unittest

from emotorad_ai.guardrails import claims_ticket

STAGING_REPLY = (
    "Stop using and stop charging the bike now. The battery is swelling and smoking. This is a safety "
    "hazard.\n\nDo not try to charge it, ride it, or keep it indoors. Move it outside to a safe open "
    "space away from people and flammable things, and leave it there.\n\nI'm raising an urgent support "
    "ticket for you right now."
)


class TicketClaimTests(unittest.TestCase):
    def test_claims(self):
        for reply in (STAGING_REPLY,
                      "Ticket EM-00001 has been raised.",
                      "I've opened a ticket for the team.",
                      "I have logged a support ticket.",
                      "Sure. I'll raise a ticket for you right away.",
                      "Your ticket number is EM-00009."):
            self.assertTrue(claims_ticket(reply), reply)

    def test_offers_questions_negations_and_conditions_are_not(self):
        for reply in ("I can raise a ticket if you'd like.",
                      "Shall I raise a ticket?",
                      "Would you like me to raise a support ticket?",
                      "If the light stays off, I'll raise a ticket.",
                      "No ticket has been raised yet. Shall I raise one?",
                      "Once a ticket has been raised, the team usually calls within a day.",
                      "I haven't raised a ticket.",
                      "Stop charging it if you ever see smoke.",
                      ""):
            self.assertFalse(claims_ticket(reply), reply)
