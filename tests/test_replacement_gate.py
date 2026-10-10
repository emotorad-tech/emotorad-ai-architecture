"""The order gate (spec 2026-10-10 replacement orders, section 1)."""

import unittest
from datetime import date

from emotorad_ai.fulfilment import ReplacementOrders
from emotorad_ai.tools import oms_db
from emotorad_ai.tools.mocks import PLACE_REPLACEMENT_ORDER, build_registry
from emotorad_ai.tools.registry import ToolContext

TODAY = date(2026, 10, 10)
PHONE = "+919876543210"
FRAME = "EMXP2025004417"
ADDRESS = "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006"
TYPED = ("I'm Test Rider, test.rider@example.com",)
RIDER = {"customer_name": "Test Rider", "email": "test.rider@example.com"}


def registration(bought=date(2026, 6, 1)):
    return oms_db.to_record({"frame_number": FRAME, "product_name": "EMX Plus", "purchase_date": bought,
                             "full_address": ADDRESS, "invoice_image": None, "status": None}, TODAY)


def order_bike(bought=date(2026, 6, 1)):
    return oms_db.order_to_record({"frame_number": FRAME, "product_name": "EMX Plus", "purchase_date": bought,
                                   "order_source": "End Customer"}, TODAY)


def coverage_for(record):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: [record])
    return registry.call("lookup_warranty_record", {}, ToolContext(conversation_id="c", phone=PHONE))


def place(record, verified="battery", part="battery", messages=TYPED, **extra):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: [record],
                              replacement_orders=ReplacementOrders())
    context = ToolContext(conversation_id="c1", phone=PHONE, late={
        "evidence_seen": lambda: True,
        "evidence_verified": lambda: verified,
        "coverage_result": lambda: coverage_for(record),
        "customer_messages": lambda: messages,
    })
    # Task 2 adds the rider's details here: dict(..., **RIDER).
    args = {"frame_number": FRAME, "part": part, "use_record_address": True, "idempotency_key": "k"}
    args.update(extra)
    return registry.call(PLACE_REPLACEMENT_ORDER, args, context)


def placed(envelope):
    """The envelope of a success carries data and no error (there is no "ok" key)."""
    return "error" not in envelope and bool(envelope.get("data"))


def code_of(envelope):
    return (envelope.get("error") or {}).get("code")


class EvidenceTests(unittest.TestCase):
    def test_no_verified_fault_is_refused(self):
        self.assertEqual(code_of(place(registration(), verified=None)), "evidence_not_verified")

    def test_a_motor_verdict_does_not_order_a_battery(self):
        self.assertEqual(code_of(place(registration(), verified="motor")), "evidence_not_verified")

    def test_a_battery_verdict_orders_a_charger(self):
        self.assertTrue(placed(place(registration(), verified="battery", part="charger")))

    def test_any_is_the_evidence_check_off(self):
        self.assertTrue(placed(place(registration(), verified="any")))


class RegistrationTests(unittest.TestCase):
    def test_an_order_bike_is_refused(self):
        self.assertEqual(code_of(place(order_bike())), "warranty_not_from_registration")

    def test_an_undated_registration_is_refused(self):
        self.assertIn(code_of(place(registration(bought=None))),
                      ("warranty_not_from_registration", "coverage_undetermined"))

    def test_a_record_with_no_ownership_source_is_refused(self):
        record = dict(registration())
        del record["ownership_source"]
        self.assertEqual(code_of(place(record)), "warranty_not_from_registration")

    def test_the_lookup_carries_the_source_onto_each_bike(self):
        [bike] = coverage_for(registration())["data"]["bikes"]
        self.assertEqual(bike["ownership_source"], "oms_purchase")


class PartCoverTests(unittest.TestCase):
    def test_a_charger_past_its_own_term_is_refused_though_the_battery_is_covered(self):
        # Bought 2026-03-01: charger (6 months) ended 2026-08-31, battery (12) runs to 2027-02-28.
        record = registration(bought=date(2026, 3, 1))
        self.assertEqual(code_of(place(record, part="charger")), "part_not_in_warranty")
        self.assertTrue(placed(place(record, part="battery")))

    def test_a_battery_past_its_term_is_refused(self):
        self.assertEqual(code_of(place(registration(bought=date(2025, 1, 1)))), "part_not_in_warranty")


class OcrTests(unittest.TestCase):
    def test_a_bike_dated_only_by_an_invoice_reading_is_never_ordered(self):
        # OCR writes invoice_readings and a ticket, never the record's date.
        self.assertIn(code_of(place(registration(bought=None))),
                      ("warranty_not_from_registration", "coverage_undetermined"))


if __name__ == "__main__":
    unittest.main()
