import unittest
import unittest.mock
from datetime import date

from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import (
    CREATE_SUPPORT_TICKET,
    LOOKUP_WARRANTY_RECORD,
    build_registry,
)
from emotorad_ai.tools.registry import ToolContext, ToolError, ToolRegistry, is_error

TODAY = date(2026, 7, 28)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(today=TODAY)
        self.ctx = ToolContext(conversation_id="c1", phone="+919876543210")

    def test_the_injected_phone_is_not_in_the_model_facing_schema(self):
        schema = self.registry.specs[LOOKUP_WARRANTY_RECORD].schema()
        self.assertNotIn("phone", schema["input_schema"]["properties"])

    def test_the_model_cannot_look_up_a_number_the_platform_did_not_resolve(self):
        envelope = self.registry.call(
            LOOKUP_WARRANTY_RECORD, {"phone": "+919812345678"}, self.ctx
        )
        self.assertEqual(envelope["data"]["customer_name"], "Ananya Rao")

    def test_missing_identity_is_an_error_not_a_crash(self):
        envelope = self.registry.call(
            LOOKUP_WARRANTY_RECORD, {}, ToolContext(conversation_id="c1")
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "missing_identity")

    def test_one_call_returns_ownership_and_coverage_together(self):
        data = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, self.ctx)["data"]
        bike = data["bikes"][0]
        self.assertEqual(data["bike_count"], 1)
        self.assertEqual(bike["frame_number"], "EMXP2025004417")
        self.assertTrue(bike["in_warranty"])
        self.assertEqual(bike["warranty_start"], "2025-03-14")
        self.assertEqual(bike["warranty_end"], "2027-03-14")
        self.assertEqual(bike["term_source"], "provisional")

    def test_coverage_runs_from_purchase_date_never_from_registration(self):
        # created_at is 2025-03-20; purchase_date is 2025-03-14. Computing from
        # the former would hand out coverage the customer never bought.
        bike = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, self.ctx)["data"]["bikes"][0]
        self.assertEqual(bike["warranty_start"], "2025-03-14")

    def test_an_out_of_warranty_bike_says_so(self):
        ctx = ToolContext(conversation_id="c1", phone="+919812345678")
        bike = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, ctx)["data"]["bikes"][0]
        self.assertFalse(bike["in_warranty"])
        self.assertEqual(bike["months_remaining"], 0)

    def test_multi_bike_returns_every_bike_rather_than_picking_one(self):
        ctx = ToolContext(conversation_id="c1", phone="+919700000001")
        data = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, ctx)["data"]
        self.assertEqual(data["bike_count"], 3)
        self.assertEqual(len({b["frame_number"] for b in data["bikes"]}), 3)

    def test_a_missing_purchase_date_asks_for_the_invoice_instead_of_guessing(self):
        ctx = ToolContext(conversation_id="c1", phone="+919700000002")
        bike = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, ctx)["data"]["bikes"][0]
        self.assertIsNone(bike["in_warranty"])
        self.assertEqual(bike["coverage_status"], "purchase_date_missing")
        self.assertEqual(bike["remedy"], "collect_purchase_proof")
        self.assertNotIn("warranty_end", bike)

    def test_no_record_is_a_registration_path_not_a_failure(self):
        ctx = ToolContext(conversation_id="c1", phone=fixtures.PHONE_WITH_NO_RECORD)
        envelope = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, ctx)
        self.assertEqual(envelope["error"]["code"], "no_warranty_record")
        self.assertEqual(envelope["error"]["remedy"], "late_warranty_registration")
        self.assertFalse(envelope["error"]["retryable"])

    def test_an_oms_outage_never_looks_like_no_record(self):
        # Conflating these tells a registered customer to re-register, or tells
        # an unregistered one to come back later forever.
        down = build_registry(today=TODAY, oms_available=False)
        envelope = down.call(LOOKUP_WARRANTY_RECORD, {}, self.ctx)
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")
        self.assertTrue(envelope["error"]["retryable"])
        self.assertNotIn("remedy", envelope["error"])

    def test_empty_upstream_strings_are_normalised_to_null(self):
        # product_color is "" in the real payload, not null.
        bike = self.registry.call(LOOKUP_WARRANTY_RECORD, {}, self.ctx)["data"]["bikes"][0]
        self.assertIsNone(bike["product_color"])

    def test_a_ticket_cannot_name_a_bike_the_customer_does_not_own(self):
        envelope = self.registry.call(
            CREATE_SUPPORT_TICKET,
            {
                "category": "battery_charging",
                "description": "won't charge",
                "severity": "normal",
                "idempotency_key": "k-frame",
                "frame_number": "DDL32022119302",  # Rohit's bike, not Ananya's
            },
            self.ctx,
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "frame_number_not_owned")

    def test_a_multi_bike_ticket_must_say_which_bike(self):
        ctx = ToolContext(conversation_id="c1", phone="+919700000001")
        envelope = self.registry.call(
            CREATE_SUPPORT_TICKET,
            {
                "category": "battery_charging",
                "description": "won't charge",
                "severity": "normal",
                "idempotency_key": "k-multi",
            },
            ctx,
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "frame_number_required")

    def test_writes_require_an_idempotency_key(self):
        envelope = self.registry.call(
            CREATE_SUPPORT_TICKET,
            {"category": "battery_charging", "description": "x", "severity": "normal"},
            self.ctx,
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "missing_idempotency_key")

    def test_retried_write_returns_the_first_ticket_rather_than_a_duplicate(self):
        arguments = {
            "category": "battery_charging",
            "description": "will not charge",
            "severity": "normal",
            "idempotency_key": "abc123",
        }
        first = self.registry.call(CREATE_SUPPORT_TICKET, dict(arguments), self.ctx)
        second = self.registry.call(CREATE_SUPPORT_TICKET, dict(arguments), self.ctx)
        self.assertEqual(first["data"]["ticket_id"], second["data"]["ticket_id"])
        self.assertEqual(len(self.registry.tickets.tickets), 1)

    def test_invalid_enum_values_come_back_as_error_envelopes(self):
        envelope = self.registry.call(
            CREATE_SUPPORT_TICKET,
            {
                "category": "refund_please",
                "description": "x",
                "severity": "normal",
                "idempotency_key": "k1",
            },
            self.ctx,
        )
        self.assertTrue(is_error(envelope))
        self.assertEqual(envelope["error"]["code"], "invalid_category")

    def test_unknown_tool_and_broken_tool_never_raise(self):
        self.assertTrue(is_error(self.registry.call("nope", {}, self.ctx)))

        registry = ToolRegistry()

        @registry.register("boom", "explodes", parameters={})
        def boom():
            raise RuntimeError("kaboom")

        envelope = registry.call("boom", {}, self.ctx)
        self.assertEqual(envelope["error"]["code"], "tool_exception")
        self.assertTrue(envelope["error"]["retryable"])

    def test_tool_errors_carry_their_own_code(self):
        registry = ToolRegistry()

        @registry.register("nope", "always fails", parameters={})
        def nope():
            raise ToolError("upstream_down", "OMS unavailable", retryable=True)

        envelope = registry.call("nope", {}, self.ctx)
        self.assertEqual(envelope["error"]["code"], "upstream_down")

    def test_diagnostics_tool_absent_unless_telematics_exist(self):
        self.assertNotIn("get_battery_diagnostics", self.registry.specs)
        self.assertIn("get_battery_diagnostics", build_registry(diagnostics_available=True).specs)


if __name__ == "__main__":
    unittest.main()
