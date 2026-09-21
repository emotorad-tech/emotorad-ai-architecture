"""A reply may only name an order the tool actually placed.

Same shape as the coverage post-check and for the same reason: the tool
running proves the tool ran, not that the reply matches what it returned. A
model that invents "your order RO-00042 is on its way" has done the Air Canada
thing with a shipment instead of a refund.
"""

import unittest

from emotorad_ai.guardrails import check_order_claim

PLACED = [{"data": {"order_id": "RO-00007", "status": "approved"}}]
NOTHING: list = []


class OrderClaimTests(unittest.TestCase):
    def test_a_reply_naming_no_order_passes(self):
        self.assertFalse(check_order_claim("I have raised a ticket for you.", NOTHING).blocked)

    def test_the_order_the_tool_placed_may_be_named(self):
        self.assertFalse(check_order_claim("Done. Order RO-00007 is on its way.", PLACED).blocked)

    def test_an_order_no_tool_placed_is_blocked(self):
        check = check_order_claim("Done. Order RO-00042 is on its way.", NOTHING)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "order_claim_without_tool_result")
        self.assertEqual(check.claimed, "RO-00042")

    def test_a_different_order_from_the_one_placed_is_blocked(self):
        check = check_order_claim("Done. Order RO-00042 is on its way.", PLACED)
        self.assertTrue(check.blocked)
        self.assertEqual(check.claimed, "RO-00042")

    def test_ticket_ids_are_not_order_ids(self):
        """EM- is a ticket. The coverage of this check is RO- alone."""
        self.assertFalse(check_order_claim("Ticket EM-00001 is open.", NOTHING).blocked)

    def test_an_in_flight_report_may_name_the_existing_order(self):
        existing = [{"data": {"order_id": "RO-00003", "already_placed": True}}]
        self.assertFalse(check_order_claim("That is already on its way as RO-00003.", existing).blocked)


IN_FLIGHT = [{"data": {"order_id": "RO-00001", "already_placed": True,
                       "delivery_address": "A1102, Park View City 1, Gurugram, Haryana, 122018"}}]


class AlreadyPlacedTests(unittest.TestCase):
    """On 2026-09-21 (conversation 32e76ff3) the tool returned the order placed
    that morning and the model said "That's done. Your replacement battery is
    ordered", to an address the customer had not confirmed in that
    conversation. An existing order must be reported as existing."""

    def test_reporting_an_existing_order_as_newly_placed_is_blocked(self):
        check = check_order_claim(
            "That's done. Your replacement battery is ordered, order RO-00001, going to "
            "A1102, Park View City 1, Gurugram, Haryana, 122018.", IN_FLIGHT)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "existing_order_reported_as_new")
        self.assertEqual(check.claimed, "RO-00001")

    def test_saying_it_was_placed_earlier_passes(self):
        self.assertFalse(check_order_claim(
            "A replacement battery was already placed earlier today, order RO-00001, going to "
            "A1102, Park View City 1, Gurugram, Haryana, 122018. Do you want that address changed?",
            IN_FLIGHT).blocked)

    def test_hindi_acknowledgement_passes(self):
        self.assertFalse(check_order_claim(
            "Aapka replacement order RO-00001 pehle se hi place ho chuka hai.", IN_FLIGHT).blocked)

    def test_a_reply_that_does_not_mention_the_order_at_all_is_blocked(self):
        """Silence is the same lie: the customer gave an address and heard nothing back."""
        check = check_order_claim("Done, that's on its way.", IN_FLIGHT)
        self.assertTrue(check.blocked)
        self.assertEqual(check.reason, "existing_order_reported_as_new")

    def test_an_order_placed_now_is_not_held_to_this(self):
        placed_now = [{"data": {"order_id": "RO-00002", "already_placed": False}}]
        self.assertFalse(check_order_claim("Done. Order RO-00002 is on its way.", placed_now).blocked)


if __name__ == "__main__":
    unittest.main()
