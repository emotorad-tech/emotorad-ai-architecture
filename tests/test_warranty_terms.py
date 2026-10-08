"""Warranty from the purchase date, per part (spec 2026-10-08, section 2)."""

import unittest
from datetime import date

from emotorad_ai.warranty_terms import LEAD_PART, TERMS, coverage, valid_until


class TermsTests(unittest.TestCase):
    def test_the_terms_are_the_persons(self):
        self.assertEqual(TERMS, {"display": 6, "charger": 6, "controller": 12, "battery": 12, "motor": 12,
                                 "frame": 60})
        self.assertEqual(LEAD_PART, "battery")

    def test_cover_ends_the_day_before_the_same_date(self):
        self.assertEqual(valid_until(date(2025, 3, 12), 12), date(2026, 3, 11))
        self.assertEqual(valid_until(date(2025, 1, 31), 6), date(2025, 7, 30))
        self.assertEqual(valid_until(date(2024, 2, 29), 12), date(2025, 2, 27))
        self.assertEqual(valid_until(date(2025, 8, 31), 6), date(2026, 2, 27))

    def test_each_part_is_active_until_its_own_end(self):
        result = coverage(date(2025, 3, 12), today=date(2025, 10, 1))
        parts = {p["component"]: p for p in result["components"]}
        self.assertTrue(parts["battery"]["active"])
        self.assertFalse(parts["display"]["active"])  # ended 11 September 2025
        self.assertEqual(parts["frame"]["validUntil"], "2030-03-11")
        self.assertEqual(result["status"], "active")

    def test_the_status_follows_the_battery(self):
        self.assertEqual(coverage(date(2024, 1, 1), today=date(2025, 6, 1))["status"], "expired")

    def test_the_last_day_is_still_covered(self):
        parts = {p["component"]: p for p in coverage(date(2025, 3, 12), today=date(2026, 3, 11))["components"]}
        self.assertTrue(parts["battery"]["active"])

    def test_no_purchase_date_is_unknown_with_the_remedy(self):
        self.assertEqual(coverage(None, today=date(2025, 6, 1)),
                         {"status": "unknown", "remedy": "collect_purchase_proof", "components": []})


if __name__ == "__main__":
    unittest.main()
