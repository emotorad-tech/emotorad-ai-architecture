"""Replacement fulfilment: the deterministic half.

Spec: docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.
Everything here is code the model cannot argue with.
"""

import os
import unittest
from unittest import mock

from emotorad_ai.config import Settings, load_settings
from emotorad_ai.fulfilment import PartRule, PartsTableError, load_parts_table


class ApprovalModeSettingTests(unittest.TestCase):
    def test_defaults_to_reasonable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMOTORAD_AI_APPROVAL_MODE", None)
            self.assertEqual(load_settings().approval_mode, "reasonable")

    def test_reads_the_environment(self):
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "human"}):
            self.assertEqual(load_settings().approval_mode, "human")

    def test_an_unknown_mode_is_refused_at_startup(self):
        """A typo must not silently become 'the bot approves everything'."""
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_APPROVAL_MODE": "yolo"}):
            with self.assertRaises(ValueError):
                load_settings()


class PartsTableTests(unittest.TestCase):
    def test_the_shipped_table_loads(self):
        table = load_parts_table()
        self.assertIn("battery", table)
        self.assertIn("motor", table)

    def test_battery_needs_no_technician_and_is_not_asked(self):
        rule = load_parts_table()["battery"]
        self.assertEqual(rule, PartRule(part="battery", technician=False, ask=False))

    def test_display_needs_no_technician_but_the_customer_is_asked(self):
        rule = load_parts_table()["display"]
        self.assertFalse(rule.technician)
        self.assertTrue(rule.ask)

    def test_motor_needs_a_technician(self):
        self.assertTrue(load_parts_table()["motor"].technician)

    def test_every_part_the_business_named_is_present(self):
        expected = {"battery", "charger", "display", "seat", "motor", "controller", "brakes", "suspension"}
        self.assertEqual(set(load_parts_table()), expected)

    def test_a_malformed_row_fails_at_load(self):
        """The same rule as knowledge records: a bad file must not vanish
        silently into 'no such part'."""
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / "_replacement"
            d.mkdir()
            (d / "parts.yaml").write_text("battery:\n  technician: maybe\n")
            with self.assertRaises(PartsTableError):
                load_parts_table(tmp)


from emotorad_ai.fulfilment import IN_FLIGHT_SECONDS, ItemCodes, ReplacementOrders


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ItemCodesTests(unittest.TestCase):
    """A mock in the shape the ERP read will have: bike model plus part in,
    item code out. The real one walks Item -> BOM -> component; this one
    walks a dictionary."""

    def test_a_known_bike_and_part_resolve(self):
        self.assertEqual(ItemCodes().resolve("EMX Plus", "battery"), "BAT-EMX-48V")

    def test_an_unknown_bike_does_not(self):
        self.assertIsNone(ItemCodes().resolve("Not A Bike", "battery"))

    def test_an_unknown_part_does_not(self):
        self.assertIsNone(ItemCodes().resolve("EMX Plus", "flux capacitor"))

    def test_the_live_product_name_shape_resolves(self):
        """Real records look like 'X1 C Red-XX01EB0007/EM01AV01C19'. The model
        name is the part before the colour and the codes."""
        self.assertEqual(ItemCodes().resolve("X1 C Red-XX01EB0007/EM01AV01C19", "battery"), "BAT-X1C-36V")


class ReplacementOrdersTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.orders = ReplacementOrders(clock=self.clock)

    def test_an_order_gets_an_id_and_a_status(self):
        order = self.orders.create(frame_number="F1", part="battery", status="pending_approval")
        self.assertRegex(order["order_id"], r"^RO-\d{5}$")
        self.assertEqual(order["status"], "pending_approval")

    def test_nothing_is_in_flight_to_begin_with(self):
        self.assertIsNone(self.orders.in_flight("F1", "battery"))

    def test_a_fresh_order_is_in_flight(self):
        created = self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertEqual(self.orders.in_flight("F1", "battery")["order_id"], created["order_id"])

    def test_a_different_part_on_the_same_frame_is_not(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertIsNone(self.orders.in_flight("F1", "charger"))

    def test_the_same_part_on_a_different_frame_is_not(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.assertIsNone(self.orders.in_flight("F2", "battery"))

    def test_it_ages_out_after_48_hours(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.clock.advance(IN_FLIGHT_SECONDS + 1)
        self.assertIsNone(self.orders.in_flight("F1", "battery"))

    def test_it_is_still_in_flight_just_inside(self):
        self.orders.create(frame_number="F1", part="battery", status="approved")
        self.clock.advance(IN_FLIGHT_SECONDS - 1)
        self.assertIsNotNone(self.orders.in_flight("F1", "battery"))

    def test_approve_flips_the_status(self):
        order = self.orders.create(frame_number="F1", part="battery", status="pending_approval")
        self.assertEqual(self.orders.approve(order["order_id"])["status"], "approved")


from emotorad_ai.fulfilment import decide, is_sure

_BATTERY = PartRule(part="battery", technician=False)


class SureTests(unittest.TestCase):
    """'Sure' is four facts the runtime already holds, never the model's own
    confidence. This is the definition the approval modes stand on."""

    def test_all_four_facts_make_it_sure(self):
        self.assertTrue(is_sure(True, True, "BAT-EMX-48V", _BATTERY))

    def test_no_photo_is_not_sure(self):
        self.assertFalse(is_sure(False, True, "BAT-EMX-48V", _BATTERY))

    def test_coverage_undetermined_is_not_sure(self):
        self.assertFalse(is_sure(True, None, "BAT-EMX-48V", _BATTERY))

    def test_out_of_warranty_is_not_sure_in_this_build(self):
        """Chargeable is build two. Until then it is not a case the bot may
        approve on its own."""
        self.assertFalse(is_sure(True, False, "BAT-EMX-48V", _BATTERY))

    def test_no_item_code_is_not_sure(self):
        self.assertFalse(is_sure(True, True, None, _BATTERY))

    def test_a_part_outside_the_table_is_not_sure(self):
        self.assertFalse(is_sure(True, True, "X", None))


class DecideTests(unittest.TestCase):
    def test_bot_mode_approves_everything(self):
        self.assertEqual(decide(True, "bot"), "approved")
        self.assertEqual(decide(False, "bot"), "approved")

    def test_reasonable_mode_approves_only_sure(self):
        self.assertEqual(decide(True, "reasonable"), "approved")
        self.assertEqual(decide(False, "reasonable"), "pending_approval")

    def test_human_mode_approves_nothing(self):
        self.assertEqual(decide(True, "human"), "pending_approval")
        self.assertEqual(decide(False, "human"), "pending_approval")

    def test_an_unknown_mode_never_approves(self):
        """Settings refuses unknown modes at startup, but this function must
        fail closed on its own too."""
        self.assertEqual(decide(True, "yolo"), "pending_approval")


if __name__ == "__main__":
    unittest.main()
