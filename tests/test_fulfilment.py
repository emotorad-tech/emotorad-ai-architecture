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


if __name__ == "__main__":
    unittest.main()
