"""place_replacement_order, through the registry.

The one write the fulfilment flow makes. The model may name the part, name the
frame and pass back the address it confirmed with the customer. Everything
else is decided here: technician or not, item code, in flight, sure, and the
approval mode. Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md
"""

import unittest
from datetime import date

from emotorad_ai.fulfilment import ItemCodes, ReplacementOrders
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext

PHONE = "+919876543210"  # fixture: one EMX Plus, in warranty on 2026-07-28
COVERED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": True,
                               "delivery_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"}]}}
NOT_COVERED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": False}]}}
UNDETERMINED = {"data": {"bikes": [{"frame_number": "EMXP2025004417", "product_name": "EMX Plus", "in_warranty": None}]}}


def _registry(approval_mode="reasonable", orders=None):
    return build_registry(
        today=date(2026, 7, 28),
        replacement_orders=orders or ReplacementOrders(),
        item_codes=ItemCodes(),
        approval_mode=approval_mode,
    )


def _context(evidence_seen=True, coverage=COVERED):
    return ToolContext(
        conversation_id="c1", phone=PHONE,
        late={"evidence_seen": lambda: evidence_seen, "coverage_result": lambda: coverage},
    )


def _place(registry, context, **overrides):
    args = {"frame_number": "EMXP2025004417", "part": "battery",
            "confirmed_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006",
            "idempotency_key": "k-1"}
    args.update(overrides)
    return registry.call(PLACE_REPLACEMENT_ORDER, args, context)


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_store(self):
        """An agent that cannot place an order must not be told it can."""
        self.assertNotIn(PLACE_REPLACEMENT_ORDER, build_registry().specs)

    def test_present_with_a_store(self):
        self.assertIn(PLACE_REPLACEMENT_ORDER, _registry().specs)

    def test_it_is_a_write(self):
        self.assertTrue(_registry().specs[PLACE_REPLACEMENT_ORDER].write)


class HappyPathTests(unittest.TestCase):
    def test_a_sure_in_warranty_battery_is_approved_in_reasonable_mode(self):
        result = _place(_registry(), _context())
        self.assertNotIn("error", result, result)
        data = result["data"]
        self.assertRegex(data["order_id"], r"^RO-\d{5}$")
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["item_code"], "BAT-EMX-48V")
        self.assertEqual(data["delivery_address"], "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006")
        self.assertFalse(data["already_placed"])

    def test_the_order_is_recorded(self):
        orders = ReplacementOrders()
        _place(_registry(orders=orders), _context())
        self.assertIsNotNone(orders.in_flight("EMXP2025004417", "battery"))

    def test_a_new_address_the_customer_gave_is_used(self):
        result = _place(_registry(), _context(), confirmed_address="New place, Mumbai 400001")
        self.assertEqual(result["data"]["delivery_address"], "New place, Mumbai 400001")


class ApprovalModeTests(unittest.TestCase):
    def test_human_mode_leaves_it_pending(self):
        self.assertEqual(_place(_registry("human"), _context())["data"]["status"], "pending_approval")

    def test_bot_mode_approves_even_without_a_photo(self):
        self.assertEqual(_place(_registry("bot"), _context(evidence_seen=False))["data"]["status"], "approved")

    def test_reasonable_mode_holds_a_case_with_no_photo(self):
        """Not sure: no evidence. The order still exists, pending a human."""
        result = _place(_registry("reasonable"), _context(evidence_seen=False))
        self.assertNotIn("error", result)
        self.assertEqual(result["data"]["status"], "pending_approval")


class RefusalTests(unittest.TestCase):
    def test_a_technician_part_is_refused_with_the_dealer_remedy(self):
        result = _place(_registry(), _context(), part="motor")
        self.assertEqual(result["error"]["code"], "technician_required")
        self.assertEqual(result["error"]["remedy"], "dealer_visit")

    def test_an_unknown_part_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), part="flux capacitor")["error"]["code"], "part_not_identified")

    def test_out_of_warranty_is_refused_in_this_build(self):
        result = _place(_registry(), _context(coverage=NOT_COVERED))
        self.assertEqual(result["error"]["code"], "chargeable_not_supported")
        self.assertEqual(result["error"]["remedy"], "human_handoff")

    def test_undetermined_coverage_is_refused_with_the_invoice_remedy(self):
        result = _place(_registry(), _context(coverage=UNDETERMINED))
        self.assertEqual(result["error"]["code"], "coverage_undetermined")
        self.assertEqual(result["error"]["remedy"], "collect_purchase_proof")

    def test_no_coverage_lookup_at_all_is_refused(self):
        """The registry refuses before the tool runs: coverage_result is a
        required injected fact and it is None."""
        result = _place(_registry(), _context(coverage=None))
        self.assertEqual(result["error"]["code"], "missing_identity")

    def test_an_empty_address_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), confirmed_address="  ")["error"]["code"], "address_required")

    def test_a_frame_the_customer_does_not_own_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), frame_number="NOT-MINE")["error"]["code"], "frame_number_not_owned")


class InFlightTests(unittest.TestCase):
    def test_a_second_order_reports_the_first(self):
        registry = _registry()
        first = _place(registry, _context())["data"]
        second = _place(registry, _context(), idempotency_key="k-2")["data"]
        self.assertTrue(second["already_placed"])
        self.assertEqual(second["order_id"], first["order_id"])

    def test_the_same_idempotency_key_returns_the_same_envelope(self):
        registry = _registry()
        first = _place(registry, _context())
        second = _place(registry, _context())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
