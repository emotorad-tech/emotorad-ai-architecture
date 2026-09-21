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


def _context(evidence_seen=True, coverage=COVERED, customer_messages=()):
    return ToolContext(
        conversation_id="c1", phone=PHONE,
        late={
            "evidence_seen": lambda: evidence_seen,
            "coverage_result": lambda: coverage,
            "customer_messages": lambda: customer_messages,
        },
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

    def test_the_enum_offers_only_parts_this_build_can_ship(self):
        """This build ships no-technician, no-ask parts only. Technician parts
        and ask parts (display, seat: offer self-fit or dealer, not built yet)
        are both left out of what the model may name, even though the tool
        still refuses them in code if a model names one regardless."""
        schema = _registry().specs[PLACE_REPLACEMENT_ORDER].schema()
        self.assertEqual(schema["input_schema"]["properties"]["part"]["enum"], ["battery", "charger"])


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
        context = _context(customer_messages=["please send it to New place, Mumbai 400001"])
        result = _place(_registry(), context, confirmed_address="New place, Mumbai 400001")
        self.assertEqual(result["data"]["delivery_address"], "New place, Mumbai 400001")


class ItemCodeMissingTests(unittest.TestCase):
    """The spec says a not-sure case is still placed as pending_approval and
    nothing is dropped. A missing item code is one such case in every mode: it
    never refuses and never approves."""

    def _unknown_model_registry(self, approval_mode, orders=None):
        return build_registry(
            today=date(2026, 7, 28),
            replacement_orders=orders or ReplacementOrders(),
            item_codes=ItemCodes(),
            approval_mode=approval_mode,
            warranty_source=lambda phone: [
                {
                    "frame_number": "EMXP2025004417",
                    "product_name": "Unknown Model",
                    "purchase_date": "2025-03-14",
                    "full_address": "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006",
                }
            ],
        )

    def test_a_part_with_no_item_code_is_placed_pending_in_every_mode(self):
        coverage = {"data": {"bikes": [
            {"frame_number": "EMXP2025004417", "product_name": "Unknown Model", "in_warranty": True},
        ]}}
        for mode in ("bot", "reasonable", "human"):
            with self.subTest(mode=mode):
                orders = ReplacementOrders()
                registry = self._unknown_model_registry(mode, orders=orders)
                result = _place(registry, _context(coverage=coverage))
                self.assertNotIn("error", result, result)
                self.assertEqual(result["data"]["status"], "pending_approval")
                self.assertIsNone(result["data"]["item_code"])
                self.assertIsNotNone(orders.in_flight("EMXP2025004417", "battery"))


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
    def test_an_ask_part_is_refused_until_the_question_is_built(self):
        result = _place(_registry(), _context(), part="display")
        self.assertEqual(result["error"]["code"], "customer_choice_required")

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
        required injected fact and it is None. It is a conversation fact, not
        an identity field, so the refusal is missing_fact."""
        result = _place(_registry(), _context(coverage=None))
        self.assertEqual(result["error"]["code"], "missing_fact")

    def test_an_empty_address_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), confirmed_address="  ")["error"]["code"], "address_required")

    def test_a_frame_the_customer_does_not_own_is_refused(self):
        self.assertEqual(_place(_registry(), _context(), frame_number="NOT-MINE")["error"]["code"], "frame_number_not_owned")

    def test_an_address_the_customer_never_gave_is_refused(self):
        result = _place(_registry(), _context(), confirmed_address="1 Made Up Lane 122018")
        self.assertEqual(result["error"]["code"], "address_unconfirmed")

    def test_an_address_the_customer_typed_is_accepted(self):
        context = _context(customer_messages=["please send it to 9 New Road, Mumbai 400001"])
        result = _place(_registry(), context, confirmed_address="9 New Road, Mumbai 400001")
        self.assertNotIn("error", result, result)
        self.assertEqual(result["data"]["delivery_address"], "9 New Road, Mumbai 400001")

    def test_another_bikes_coverage_never_vouches_for_this_one(self):
        """The most safety-critical check in the tool. A record listing a
        different, covered bike must not make this frame look covered."""
        other_bike = {"data": {"bikes": [
            {"frame_number": "SOMEONE-ELSES", "product_name": "EMX Plus", "in_warranty": True},
        ]}}
        result = _place(_registry(), _context(coverage=other_bike))
        self.assertEqual(result["error"]["code"], "coverage_undetermined")


class AddressProvenanceTests(unittest.TestCase):
    """What went wrong live on 2026-09-20, conversation b186a5dd.

    The customer gave the address in two messages: "It's A1102 Park view
    city 1", then "122018". The model combined them. The check required the
    combined string to appear verbatim in ONE customer message, refused it
    twice, and the model dropped the pincode to get past the check. The order
    shipped with no pincode and the bot told the customer logistics would
    "confirm the pincode when they call". A refusal a model can route around
    by degrading its input is worse than no check.

    The rule now: every word of the confirmed address must have been typed by
    the customer somewhere in this conversation or be on the record, and an
    Indian delivery address carries a six-digit pincode.
    """

    STREET = "It's A1102 Park view city 1"
    PIN = "122018"

    def test_an_address_given_across_two_messages_is_accepted(self):
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN)),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)
        self.assertEqual(result["data"]["delivery_address"], "A1102 Park view city 1, 122018")

    def test_order_of_the_two_messages_does_not_matter(self):
        result = _place(
            _registry(), _context(customer_messages=(self.PIN, self.STREET)),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)

    def test_a_read_back_the_customer_agreed_to_is_accepted(self):
        """The bot restates the address and the customer says yes. That is
        exactly the confirmation the spec asks for and the old check ignored."""
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN, "Yes")),
            confirmed_address="A1102 Park view city 1, 122018",
        )
        self.assertNotIn("error", result, result)

    def test_a_word_the_customer_never_typed_is_refused(self):
        result = _place(
            _registry(), _context(customer_messages=(self.STREET, self.PIN)),
            confirmed_address="A1102 Park view city 1, Gurgaon, 122018",
        )
        self.assertEqual(result["error"]["code"], "address_unconfirmed")
        self.assertIn("gurgaon", result["error"]["message"].lower())

    def test_an_address_with_no_pincode_is_refused(self):
        """The refusal the model cannot route around by dropping something."""
        result = _place(
            _registry(), _context(customer_messages=(self.STREET,)),
            confirmed_address="A1102 Park view city 1",
        )
        self.assertEqual(result["error"]["code"], "pincode_required")

    def test_the_records_own_address_needs_no_customer_message(self):
        result = _place(_registry(), _context(customer_messages=()))
        self.assertNotIn("error", result, result)


class InFlightTests(unittest.TestCase):
    def test_a_second_order_reports_the_first(self):
        registry = _registry()
        first = _place(registry, _context())["data"]
        second = _place(registry, _context(), idempotency_key="k-2")["data"]
        self.assertTrue(second["already_placed"])
        self.assertEqual(second["order_id"], first["order_id"])

    def test_an_in_flight_report_says_when_it_was_placed(self):
        """The model has to tell the customer the order already exists, and
        "earlier today" needs a wall-clock time; placed_at is monotonic."""
        orders = ReplacementOrders(wall_clock=lambda: "2026-09-21T03:57:11+00:00")
        registry = _registry(orders=orders)
        _place(registry, _context())
        second = _place(registry, _context(), idempotency_key="k-2")["data"]
        self.assertEqual(second["placed_at_utc"], "2026-09-21T03:57:11+00:00")
        self.assertEqual(second["delivery_address"], "Flat 4B, Kalyani Nagar, Pune, Maharashtra 411006")

    def test_the_same_idempotency_key_returns_the_same_envelope(self):
        registry = _registry()
        first = _place(registry, _context())
        second = _place(registry, _context())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
