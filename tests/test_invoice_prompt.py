"""The customer agents leave the invoice to code (spec 2026-10-08, section 3):
with an invoice on file they do not ask for it; without one they ask for a
photo or PDF; they never state an invoice date or a cover end date."""

import unittest

from emotorad_ai.agents.base import INVOICE_RULE
from tests.test_staff_wording import _system_for


class InvoiceRuleTests(unittest.TestCase):
    def test_the_rule_says_what_the_agent_does_and_does_not(self):
        rule = " ".join(INVOICE_RULE.split())
        for words in ("invoice_on_file", "do not ask for the invoice", "checking the invoice on file",
                      "photo or PDF", "never state an invoice date or a cover end date yourself"):
            self.assertIn(words, rule)

    def test_every_customer_agent_gets_it(self):
        for agent in ("battery_support", "motor_support", "late_warranty_registration"):
            with self.subTest(agent=agent):
                self.assertIn(INVOICE_RULE.strip(), _system_for(agent))


if __name__ == "__main__":
    unittest.main()
