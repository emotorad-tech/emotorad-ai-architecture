"""What the backstop treats as a safety reply and as a ticket claim (spec 2026-10-01)."""

import unittest

from emotorad_ai.guardrails import claims_ticket, gives_safety_stop

STAGING_REPLY = (
    "Stop using and stop charging the bike now. The battery is swelling and smoking. This is a safety "
    "hazard.\n\nDo not try to charge it, ride it, or keep it indoors. Move it outside to a safe open "
    "space away from people and flammable things, and leave it there.\n\nI'm raising an urgent support "
    "ticket for you right now."
)


class SafetyReplyTests(unittest.TestCase):
    def test_the_staging_reply_is_one(self):
        self.assertIn("swelling", gives_safety_stop(STAGING_REPLY))

    def test_advice_is_not(self):
        for reply in ("If you ever see smoke, stop charging it.",
                      "In case the pack swells, stop using it and call us.",
                      "When it cools, stop charging at 80%. It is not swelling.",
                      "Stop using the throttle for a minute and try again.",
                      ""):
            self.assertEqual(gives_safety_stop(reply), [], reply)


class TicketClaimTests(unittest.TestCase):
    def test_claims(self):
        for reply in ("I'm raising an urgent support ticket for you right now.",
                      "Ticket EM-00001 has been raised.",
                      "I've opened a ticket for the team.",
                      "I have logged a support ticket.",
                      "I'll raise a ticket right away."):
            self.assertTrue(claims_ticket(reply), reply)

    def test_offers_are_not_claims(self):
        for reply in ("I can raise a ticket if you'd like.",
                      "Shall I raise a ticket?",
                      "Would you like me to raise a support ticket?",
                      "If the light stays off, I'll raise a ticket.",
                      ""):
            self.assertFalse(claims_ticket(reply), reply)
