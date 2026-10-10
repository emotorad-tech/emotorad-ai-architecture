"""The order gate (spec 2026-10-10 replacement orders, section 1)."""

import unittest
from datetime import date
from unittest import mock

from emotorad_ai.fulfilment import PartRule, ReplacementOrders
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
    args = dict({"frame_number": FRAME, "part": part, "use_record_address": True, "idempotency_key": "k"}, **RIDER)
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


    def test_a_part_with_no_fault_is_never_ordered_on_no_verdict(self):
        # The parts table makes the display an ask part, which would refuse
        # first; a table where it is orderable proves the gate itself.
        table = {"display": PartRule(part="display", technician=False, ask=False)}
        with mock.patch("emotorad_ai.tools.mocks.load_parts_table", return_value=table):
            self.assertEqual(code_of(place(registration(), verified=None, part="display")),
                             "evidence_not_verified")
            self.assertEqual(code_of(place(registration(), verified="battery", part="display")),
                             "evidence_not_verified")


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


class DetailsTests(unittest.TestCase):
    def test_a_name_the_rider_never_typed_is_refused(self):
        self.assertEqual(code_of(place(registration(), customer_name="Someone Else")), "customer_name_unconfirmed")

    def test_no_name_is_refused(self):
        self.assertEqual(code_of(place(registration(), customer_name=" ")), "customer_name_required")

    def test_a_malformed_email_is_refused(self):
        for email in ("test.rider", "test rider@example.com", "a@b", "@example.com"):
            with self.subTest(email=email):
                self.assertEqual(code_of(place(registration(), email=email)), "email_invalid")

    def test_an_email_the_rider_never_typed_is_refused(self):
        self.assertEqual(code_of(place(registration(), email="other@example.com")), "email_unconfirmed")

    def test_a_mobile_must_be_indian_and_typed(self):
        self.assertEqual(code_of(place(registration(), mobile="+6591234567")), "mobile_invalid")
        self.assertEqual(code_of(place(registration(), mobile="9123456780")), "mobile_invalid")
        typed = TYPED + ("call me on 91234 56780",)
        self.assertTrue(placed(place(registration(), messages=typed, mobile="9123456780")))

    def test_an_email_that_only_starts_or_ends_inside_the_typed_one_is_refused(self):
        for email in ("test.rider@example.co", "rider@example.com", "est.rider@example.com", "test.rider@example.comm"):
            with self.subTest(email=email):
                self.assertEqual(code_of(place(registration(), email=email)), "email_unconfirmed")

    def test_a_typed_email_in_another_case_passes(self):
        self.assertTrue(placed(place(registration(), email="Test.Rider@Example.COM")))

    def test_a_longer_typed_address_does_not_confirm_a_shorter_one(self):
        typed = ("I'm Test Rider, xx@b.com",)
        self.assertEqual(code_of(place(registration(), messages=typed, email="x@b.com")), "email_unconfirmed")
        self.assertTrue(placed(place(registration(), messages=typed, email="xx@b.com")))

    def test_a_typed_devanagari_name_passes(self):
        typed = ("मैं राहुल शर्मा हूँ, test.rider@example.com",)
        self.assertTrue(placed(place(registration(), messages=typed, customer_name="राहुल शर्मा")))

    def test_a_devanagari_name_never_typed_is_refused(self):
        typed = ("मैं राहुल शर्मा हूँ, test.rider@example.com",)
        self.assertEqual(code_of(place(registration(), messages=typed, customer_name="अमित वर्मा")),
                         "customer_name_unconfirmed")

    def test_a_name_with_no_letters_is_no_name(self):
        for name in ("...", "- -", "@"):
            with self.subTest(name=name):
                self.assertEqual(code_of(place(registration(), customer_name=name)), "customer_name_required")


if __name__ == "__main__":
    unittest.main()
