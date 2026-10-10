"""Pin-code centres and straight-line distance (spec 2026-10-09, section 1)."""

import unittest

from emotorad_ai import geo
from emotorad_ai.geo import PincodeCentres, build_centres, distance_km


class DistanceTests(unittest.TestCase):
    def test_known_city_pairs(self):
        pune, mumbai = (18.5204, 73.8567), (19.0760, 72.8777)
        delhi, bengaluru = (28.6139, 77.2090), (12.9716, 77.5946)
        self.assertTrue(115 <= distance_km(pune, mumbai) <= 125, distance_km(pune, mumbai))
        self.assertTrue(1730 <= distance_km(delhi, bengaluru) <= 1750, distance_km(delhi, bengaluru))
        self.assertEqual(distance_km(pune, pune), 0.0)


class BuildTests(unittest.TestCase):
    def test_the_median_of_the_offices_without_a_far_outlier(self):
        offices = [("411014", 18.560, 73.910), ("411014", 18.562, 73.912), ("411014", 18.558, 73.915),
                   ("411014", 20.50, 75.50)]  # one office's point is 300 km off
        centres = build_centres(offices, {})
        lat, lon, basis = centres["411014"]
        self.assertEqual(basis, "offices")
        self.assertAlmostEqual(lat, 18.560, places=2)
        self.assertAlmostEqual(lon, 73.912, places=2)

    def test_points_outside_india_and_malformed_pincodes_are_dropped(self):
        offices = [("110001", 0.0, 0.0), ("110001", 28.632, 77.219), ("12345", 28.6, 77.2), ("ABCDEF", 28.6, 77.2)]
        centres = build_centres(offices, {})
        self.assertEqual(set(centres), {"110001"})
        self.assertAlmostEqual(centres["110001"][0], 28.632, places=3)

    def test_a_pincode_with_no_point_takes_its_districts_centre(self):
        offices = [("411014", 18.56, 73.91), ("411001", 18.52, 73.86), ("411099", None, None)]
        district_of = {"411014": "Pune|Maharashtra", "411001": "Pune|Maharashtra", "411099": "Pune|Maharashtra"}
        centres = build_centres(offices, district_of)
        lat, lon, basis = centres["411099"]
        self.assertEqual(basis, "district")
        self.assertAlmostEqual(lat, 18.54, places=2)
        self.assertAlmostEqual(lon, 73.885, places=2)

    def test_values_are_rounded_to_four_places(self):
        centres = build_centres([("411014", 18.123456789, 73.987654321)], {})
        self.assertEqual(centres["411014"], (18.1235, 73.9877, "offices"))


class NearestTests(unittest.TestCase):
    def setUp(self):
        self.centres = PincodeCentres({"411014": (18.56, 73.91), "400054": (19.06, 72.84)})

    def test_the_nearest_centre_names_the_pincode(self):
        self.assertEqual(self.centres.nearest(18.55, 73.92), "411014")
        self.assertEqual(self.centres.nearest(19.07, 72.83), "400054")

    def test_nothing_within_fifty_km_or_outside_india_is_none(self):
        self.assertIsNone(self.centres.nearest(21.0, 79.0))  # Nagpur: far from both
        self.assertIsNone(self.centres.nearest(15.0, 65.0))  # the Arabian Sea, outside the box

    def test_centre_of_an_unknown_pincode_is_none(self):
        self.assertIsNone(self.centres.centre("999999"))
        self.assertEqual(self.centres.centre(" 411014 "), (18.56, 73.91))


class ShippedFileTests(unittest.TestCase):
    def test_the_file_covers_india_and_places_known_pincodes(self):
        centres = PincodeCentres.load()
        self.assertGreater(len(centres), 19000)
        self.assertLess(distance_km(centres.centre("110001"), (28.63, 77.22)), 10)
        self.assertLess(distance_km(centres.centre("411014"), (18.56, 73.91)), 15)

    def test_the_file_names_its_source_and_licence(self):
        head = geo.CENTRES_PATH.read_text().splitlines()[0]
        self.assertIn("data.gov.in", head)
        self.assertIn("Open Government Data", head)


if __name__ == "__main__":
    unittest.main()
