"""A shared location becomes a pincode the directory knows, or it becomes nothing.

The website chat has a "Share my location" button, the way delivery apps do.
The coordinates go to a reverse geocoder behind a seam; what comes back is
trusted only as far as the India Post directory confirms it. Coordinates are
never stored and never logged: only the derived pincode and area reach the
conversation, as the customer's own message, so the address provenance check
sees them as typed.
"""

import unittest

from emotorad_ai.address import PincodeDirectory
from emotorad_ai.location import (
    LocationResult,
    describe_location,
    resolve_location,
)


class FakeGeocoder:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def reverse(self, latitude, longitude):
        self.calls.append((latitude, longitude))
        if self.fail:
            raise RuntimeError("geocoder down")
        return self.answer


DIRECTORY = PincodeDirectory.load()


class ResolveTests(unittest.TestCase):
    def test_a_postcode_the_directory_knows_is_resolved_with_its_district_and_state(self):
        geocoder = FakeGeocoder({"postcode": "122018", "suburb": "Sector 49"})
        result = resolve_location(28.41, 77.05, geocoder, DIRECTORY)
        self.assertEqual(result, LocationResult(pincode="122018", area="Sector 49",
                                                district="Gurugram", state="Haryana"))

    def test_the_district_and_state_come_from_the_directory_not_the_geocoder(self):
        geocoder = FakeGeocoder({"postcode": "122018", "suburb": "Sector 49",
                                 "state_district": "Somewhere Else", "state": "Nowhere"})
        result = resolve_location(28.41, 77.05, geocoder, DIRECTORY)
        self.assertEqual((result.district, result.state), ("Gurugram", "Haryana"))

    def test_a_postcode_the_directory_does_not_know_is_no_result(self):
        result = resolve_location(28.41, 77.05, FakeGeocoder({"postcode": "999999"}), DIRECTORY)
        self.assertIsNone(result)

    def test_no_postcode_is_no_result(self):
        result = resolve_location(28.41, 77.05, FakeGeocoder({"suburb": "Sector 49"}), DIRECTORY)
        self.assertIsNone(result)

    def test_a_geocoder_failure_is_no_result_not_an_exception(self):
        with self.assertLogs("emotorad_ai.location", level="WARNING"):
            result = resolve_location(28.41, 77.05, FakeGeocoder(fail=True), DIRECTORY)
        self.assertIsNone(result)

    def test_the_area_falls_back_through_the_geocoders_fields(self):
        geocoder = FakeGeocoder({"postcode": "122018", "neighbourhood": "Block C"})
        self.assertEqual(resolve_location(28.41, 77.05, geocoder, DIRECTORY).area, "Block C")

    def test_a_two_state_pincode_keeps_the_first_place(self):
        """The tool asks the customer to pick when it matters; the location
        message just needs a truthful pincode."""
        result = resolve_location(28.6, 77.2, FakeGeocoder({"postcode": "110025"}), DIRECTORY)
        self.assertEqual(result.pincode, "110025")


class DescribeTests(unittest.TestCase):
    def test_a_resolved_location_reads_as_the_customers_message(self):
        result = LocationResult(pincode="122018", area="Sector 49", district="Gurugram", state="Haryana")
        self.assertEqual(
            describe_location(result),
            "I shared my location. Pincode 122018, Sector 49, Gurugram, Haryana.",
        )

    def test_no_area_is_left_out_not_blank(self):
        result = LocationResult(pincode="122018", area=None, district="Gurugram", state="Haryana")
        self.assertEqual(describe_location(result), "I shared my location. Pincode 122018, Gurugram, Haryana.")

    def test_an_unresolved_location_says_so(self):
        self.assertEqual(
            describe_location(None),
            "I shared my location, but the pincode could not be worked out from it.",
        )


if __name__ == "__main__":
    unittest.main()
