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


def registration(bought=date(2026, 6, 1), frame=FRAME):
    return oms_db.to_record({"frame_number": frame, "product_name": "EMX Plus", "purchase_date": bought,
                             "full_address": ADDRESS, "invoice_image": None, "status": None}, TODAY)


def order_bike(bought=date(2026, 6, 1)):
    return oms_db.order_to_record({"frame_number": FRAME, "product_name": "EMX Plus", "purchase_date": bought,
                                   "order_source": "End Customer"}, TODAY)


def coverage_for(*records):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: list(records))
    return registry.call("lookup_warranty_record", {}, ToolContext(conversation_id="c", phone=PHONE))


def fact(component, frame=FRAME):
    """The evidence fact the runtime injects: the fault proved and the bike it was proved on."""
    return None if component is None else {"component": component, "frame": frame}


def place(record, verified="battery", part="battery", messages=TYPED, records=None, verified_frame=FRAME,
          frame=FRAME, **extra):
    records = records or [record]
    registry = build_registry(today=TODAY, warranty_source=lambda phone: list(records),
                              replacement_orders=ReplacementOrders())
    context = ToolContext(conversation_id="c1", phone=PHONE, late={
        "evidence_seen": lambda: True,
        "evidence_verified": lambda: fact(verified, verified_frame),
        "coverage_result": lambda: coverage_for(*records),
        "customer_messages": lambda: messages,
    })
    args = dict({"frame_number": frame, "part": part, "use_record_address": True, "idempotency_key": "k"}, **RIDER)
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

    def test_evidence_for_one_bike_never_orders_another(self):
        # Finding 1 of the whole-branch review: a passing video for bike A let
        # bike B's battery be ordered, since any owned frame was accepted.
        other = "EMXP2025009999"
        bikes = [registration(), registration(frame=other)]
        self.assertEqual(code_of(place(None, records=bikes, verified_frame=FRAME, frame=other)),
                         "evidence_not_verified")
        self.assertTrue(placed(place(None, records=bikes, verified_frame=FRAME, frame=FRAME)))

    def test_the_evidence_frame_is_compared_upper_cased_without_spaces(self):
        self.assertTrue(placed(place(registration(), verified_frame="emxp 2025 004417")))

    def test_evidence_with_no_bike_orders_nothing(self):
        self.assertEqual(code_of(place(registration(), verified_frame=None)), "evidence_not_verified")
        self.assertEqual(code_of(place(registration(), verified="any", verified_frame=None)),
                         "evidence_not_verified")

    def test_a_bare_component_string_is_not_evidence(self):
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [registration()],
                                  replacement_orders=ReplacementOrders())
        context = ToolContext(conversation_id="c1", phone=PHONE, late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: "battery",
            "coverage_result": lambda: coverage_for(registration()), "customer_messages": lambda: TYPED})
        args = dict({"frame_number": FRAME, "part": "battery", "use_record_address": True, "idempotency_key": "k"},
                    **RIDER)
        self.assertEqual(code_of(registry.call(PLACE_REPLACEMENT_ORDER, args, context)), "evidence_not_verified")


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


class CoverageUndeterminedTests(unittest.TestCase):
    def test_a_frame_missing_from_the_lookup_is_undetermined(self):
        # The rider owns the bike, but the lookup the chat holds does not list it.
        other = "EMXP2025009999"
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [registration(),
                                                                              registration(frame=other)],
                                  replacement_orders=ReplacementOrders())
        context = ToolContext(conversation_id="c1", phone=PHONE, late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: fact("battery", other),
            "coverage_result": lambda: coverage_for(registration()), "customer_messages": lambda: TYPED})
        envelope = registry.call(PLACE_REPLACEMENT_ORDER, dict(
            {"frame_number": other, "part": "battery", "use_record_address": True, "idempotency_key": "k"},
            **RIDER), context)
        self.assertEqual(code_of(envelope), "coverage_undetermined")
        self.assertEqual(envelope["error"]["remedy"], "collect_purchase_proof")

    def test_a_part_with_no_component_entry_is_undetermined(self):
        record = registration()
        coverage = coverage_for(record)
        for bike in coverage["data"]["bikes"]:
            bike["components"] = [c for c in bike.get("components") or [] if c.get("component") != "battery"]
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record],
                                  replacement_orders=ReplacementOrders())
        context = ToolContext(conversation_id="c1", phone=PHONE, late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: fact("battery"),
            "coverage_result": lambda: coverage, "customer_messages": lambda: TYPED})
        envelope = registry.call(PLACE_REPLACEMENT_ORDER, dict(
            {"frame_number": FRAME, "part": "battery", "use_record_address": True, "idempotency_key": "k"},
            **RIDER), context)
        self.assertEqual(code_of(envelope), "coverage_undetermined")
        self.assertEqual(envelope["error"]["remedy"], "support_ticket")


class OcrTests(unittest.TestCase):
    def test_a_bike_dated_only_by_an_invoice_reading_is_never_ordered(self):
        # OCR writes invoice_readings and a ticket, never the record's date.
        self.assertIn(code_of(place(registration(bought=None))),
                      ("warranty_not_from_registration", "coverage_undetermined"))


class DetailsTests(unittest.TestCase):
    def test_a_name_the_rider_never_typed_is_refused(self):
        self.assertEqual(code_of(place(registration(), customer_name="Someone Else")), "customer_name_unconfirmed")

    def test_the_refusal_never_repeats_the_words_it_rejected(self):
        # Finding 8: the message is logged with the tool call, and the name's
        # values are never logged (spec section 2). It says how many, not which.
        envelope = place(registration(), customer_name="Test Zanzibar Quixote")
        self.assertEqual(code_of(envelope), "customer_name_unconfirmed")
        message = envelope["error"]["message"]
        for word in ("Zanzibar", "Quixote", "zanzibar", "quixote"):
            self.assertNotIn(word, message)
        self.assertIn("2 words", message)
        one = place(registration(), customer_name="Test Zanzibar")["error"]["message"]
        self.assertIn("1 word ", one)
        self.assertNotIn("Zanzibar", one)

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


class LedgerThroughTheToolTests(unittest.TestCase):
    def test_the_same_bike_and_part_twice_is_one_order(self):
        orders = ReplacementOrders()
        record = registration()
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record], replacement_orders=orders)
        for cid in ("c1", "c2"):
            context = ToolContext(conversation_id=cid, phone=PHONE, late={
                "evidence_seen": lambda: True, "evidence_verified": lambda: fact("battery"),
                "coverage_result": lambda: coverage_for(record), "customer_messages": lambda: TYPED})
            result = registry.call(PLACE_REPLACEMENT_ORDER, dict(
                {"frame_number": FRAME, "part": "battery", "use_record_address": True,
                 "idempotency_key": "k-" + cid}, **RIDER), context)
        self.assertTrue(result["data"]["already_placed"])
        self.assertEqual(result["data"]["order_id"], "RO-1000001")

    def test_the_address_of_an_existing_order_is_told_only_to_its_own_phone(self):
        orders = ReplacementOrders()
        record = registration()
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record], replacement_orders=orders)

        def call(phone, key):
            context = ToolContext(conversation_id="c-" + key, phone=phone, late={
                "evidence_seen": lambda: True, "evidence_verified": lambda: fact("battery"),
                "coverage_result": lambda: coverage_for(record), "customer_messages": lambda: TYPED})
            return registry.call(PLACE_REPLACEMENT_ORDER, dict(
                {"frame_number": FRAME, "part": "battery", "use_record_address": True,
                 "idempotency_key": key}, **RIDER), context)

        first = call(PHONE, "k1")["data"]
        self.assertFalse(first["already_placed"])
        same = call("+91 98765 43210", "k2")["data"]
        self.assertTrue(same["already_placed"])
        self.assertEqual(same["delivery_address"], ADDRESS)
        self.assertIn("placed_at_utc", same)
        other = call("+919123456789", "k3")["data"]
        self.assertTrue(other["already_placed"])
        self.assertEqual(other["order_id"], first["order_id"])
        self.assertNotIn("delivery_address", other)
        self.assertNotIn("placed_at_utc", other)
        self.assertIn("already placed from another number", other["note"])

    def test_with_the_switch_off_or_no_product_id_the_order_is_recorded_not_queued(self):
        self.assertEqual(place(registration())["data"]["status"], "recorded")

    def test_with_the_switch_on_and_a_product_id_the_order_is_queued(self):
        from emotorad_ai.fulfilment import ProductIds

        record = registration()
        registry = build_registry(today=TODAY, warranty_source=lambda phone: [record],
                                  replacement_orders=ReplacementOrders(), orders_live=True,
                                  product_ids=ProductIds({"EMX Plus": {"battery": "uuid-1"}}))
        context = ToolContext(conversation_id="c1", phone=PHONE, late={
            "evidence_seen": lambda: True, "evidence_verified": lambda: fact("battery"),
            "coverage_result": lambda: coverage_for(record), "customer_messages": lambda: TYPED})
        result = registry.call(PLACE_REPLACEMENT_ORDER, dict(
            {"frame_number": FRAME, "part": "battery", "use_record_address": True, "idempotency_key": "k"},
            **RIDER), context)
        self.assertEqual(result["data"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
