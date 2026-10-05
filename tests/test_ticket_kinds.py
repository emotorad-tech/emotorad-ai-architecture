"""Ticket kinds, urgency, subject labels, the two reference shapes, and the
clock the ticket store keeps time with (plan Task 2)."""

import unittest
from datetime import datetime, timedelta, timezone

from emotorad_ai.tickets import kinds
from emotorad_ai.tickets.clock import iso, now_iso, parse, plus
from emotorad_ai.tickets.kinds import (
    FIRST_DESK_NUMBER, KINDS, desk_reference, is_desk_reference, is_urgent, subject_label,
)

T0 = "2026-10-05T10:00:00.000000+00:00"
IST = timezone(timedelta(hours=5, minutes=30))


class KindTests(unittest.TestCase):
    def test_the_six_kinds(self):
        self.assertEqual(KINDS, ("support", "safety", "handover", "lockout", "intake", "warranty_proof"))

    def test_the_safety_kind_or_a_battery_safety_category_is_urgent(self):
        self.assertTrue(is_urgent("safety", None))
        self.assertTrue(is_urgent("safety", "battery_charging"))
        self.assertTrue(is_urgent("support", "battery_safety"))
        self.assertFalse(is_urgent("support", "battery_charging"))
        for kind in ("handover", "lockout", "intake", "warranty_proof"):
            self.assertFalse(is_urgent(kind, None), kind)

    def test_subject_labels(self):
        self.assertEqual([subject_label(kind, None) for kind in KINDS[1:]],
                         ["SAFETY", "Asked for a person", "Could not verify", "Unverified customer",
                          "Late warranty registration"])
        self.assertEqual(subject_label("support", "battery_charging"), "Battery: charging")
        self.assertEqual(subject_label("support", "battery_range"), "Battery: range")
        self.assertEqual(subject_label("support", "battery_power"), "Battery: power")
        self.assertEqual(subject_label("support", "battery_safety"), "Battery: safety")
        self.assertEqual(subject_label("support", "other"), "Other")
        self.assertEqual(subject_label("support", None), "Support")
        self.assertEqual(subject_label("support", "late_warranty_registration"), "Late warranty registration")
        self.assertEqual(subject_label("safety", "battery_charging"), "SAFETY")  # the kind decides
        with self.assertRaises(ValueError):
            subject_label("complaint", None)

    def test_the_thresholds(self):
        self.assertEqual((kinds.URGENT_LATE_SECONDS, kinds.STUCK_SECONDS), (600, 86400))


class ReferenceTests(unittest.TestCase):
    def test_desk_references_start_at_seven_digits(self):
        self.assertEqual(FIRST_DESK_NUMBER, 1000001)
        self.assertEqual(desk_reference(FIRST_DESK_NUMBER), "EM-1000001")

    def test_only_an_em_reference_of_seven_or_more_digits_is_desks(self):
        for desk in ("EM-1000001", "EM-1000002", "EM-12345678"):
            self.assertTrue(is_desk_reference(desk), desk)
        for other in ("EM-00001", "EM-99999", "EM-123456", "BK-1000001", "RO-1000001", "EM-1000001 ",
                      " EM-1000001", "EM-1000001x", "em-1000001", "", None):
            self.assertFalse(is_desk_reference(other), other)

    def test_devanagari_digits_are_not_a_desk_reference(self):
        # Python's \d matches these; the pattern uses [0-9] so they never pass.
        self.assertFalse(is_desk_reference("EM-१०००००१"))


class ClockTests(unittest.TestCase):
    def test_now_is_utc_with_microseconds(self):
        self.assertRegex(now_iso(), r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00$")

    def test_a_whole_second_still_carries_microseconds(self):
        self.assertEqual(iso(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)), T0)

    def test_another_zone_is_written_as_utc_and_a_naive_time_is_taken_as_utc(self):
        self.assertEqual(iso(datetime(2026, 10, 5, 15, 30, tzinfo=IST)), T0)
        self.assertEqual(iso(datetime(2026, 10, 5, 10, 0)), T0)

    def test_plus_moves_either_way_in_the_same_format(self):
        self.assertEqual(plus(T0, 120), "2026-10-05T10:02:00.000000+00:00")
        self.assertEqual(plus(T0, -600), "2026-10-05T09:50:00.000000+00:00")
        self.assertEqual(plus(T0, 0.5), "2026-10-05T10:00:00.500000+00:00")

    def test_parse_reads_the_stored_form_back(self):
        self.assertEqual(parse(T0), datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(parse("2026-10-05T10:00:00"), datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))

    def test_string_order_is_time_order_across_midnight(self):
        before = "2026-10-05T23:59:59.999999+00:00"
        after = plus(before, 0.000001)
        self.assertEqual(after, "2026-10-06T00:00:00.000000+00:00")
        self.assertLess(before, after)


if __name__ == "__main__":
    unittest.main()
