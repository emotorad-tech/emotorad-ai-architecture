"""The delivery address is structured, and the city and state come from the pincode.

On 2026-09-21 the model shipped "A1102, Park View City 1, 122018" with no city or
state, because nothing required one. A pincode fixes the district and state, so
the customer is never asked to type what the code already says. The India Post
directory lives in the repo as one row per pincode.
"""

import unittest

from emotorad_ai.address import (
    Address,
    AddressError,
    PincodeDirectory,
    Place,
    assemble,
    parse_address,
)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = PincodeDirectory.load()

    def test_a_known_pincode_resolves_to_its_district_and_state(self):
        self.assertEqual(self.directory.lookup("122018"), [Place("Gurugram", "Haryana")])

    def test_an_unknown_pincode_resolves_to_nothing(self):
        self.assertEqual(self.directory.lookup("999999"), [])

    def test_a_pincode_on_a_state_border_lists_every_state(self):
        """A few hundred pincodes span two states in the directory (110025 is served from Delhi and, per India Post, Budaun). The customer picks."""
        states = {p.state for p in self.directory.lookup("110025")}
        self.assertGreater(len(states), 1, states)

    def test_the_most_served_district_leads(self):
        """110003 is served from three Delhi districts; South East has the most offices."""
        self.assertEqual(self.directory.lookup("110003")[0], Place("South East", "Delhi"))

    def test_the_directory_is_complete(self):
        self.assertGreater(len(self.directory), 19000)

    def test_the_file_is_loaded_once(self):
        self.assertIs(PincodeDirectory.load(), PincodeDirectory.load())


class ParseTests(unittest.TestCase):
    def test_all_required_fields_present(self):
        address = parse_address({
            "house_or_flat": "A1102", "building_or_street": "Park View City 1",
            "area": "Sector 49", "pincode": "122018",
        })
        self.assertEqual(address.pincode, "122018")
        self.assertIsNone(address.landmark)

    def test_a_blank_required_field_names_the_field(self):
        with self.assertRaises(AddressError) as caught:
            parse_address({"house_or_flat": "", "building_or_street": "Park View City 1",
                           "area": "Sector 49", "pincode": "122018"})
        self.assertEqual(caught.exception.code, "address_incomplete")
        self.assertIn("house_or_flat", caught.exception.message)

    def test_a_missing_field_is_the_same_as_a_blank_one(self):
        with self.assertRaises(AddressError) as caught:
            parse_address({"house_or_flat": "A1102", "pincode": "122018"})
        self.assertEqual(caught.exception.code, "address_incomplete")

    def test_a_malformed_pincode_is_refused(self):
        with self.assertRaises(AddressError) as caught:
            parse_address({"house_or_flat": "A1102", "building_or_street": "Park View City 1",
                           "area": "Sector 49", "pincode": "12201"})
        self.assertEqual(caught.exception.code, "pincode_invalid")

    def test_whitespace_is_trimmed(self):
        address = parse_address({"house_or_flat": " A1102 ", "building_or_street": "Park View City 1 ",
                                 "area": " Sector 49", "pincode": " 122018 "})
        self.assertEqual(address.house_or_flat, "A1102")
        self.assertEqual(address.pincode, "122018")


class AssembleTests(unittest.TestCase):
    def test_the_line_reads_as_a_postal_address(self):
        address = Address("A1102", "Park View City 1", "Sector 49", None, "122018")
        self.assertEqual(
            assemble(address, Place("Gurugram", "Haryana")),
            "A1102, Park View City 1, Sector 49, Gurugram, Haryana, 122018",
        )

    def test_a_landmark_sits_after_the_area(self):
        address = Address("A1102", "Park View City 1", "Sector 49", "near Omaxe Mall", "122018")
        self.assertEqual(
            assemble(address, Place("Gurugram", "Haryana")),
            "A1102, Park View City 1, Sector 49, near Omaxe Mall, Gurugram, Haryana, 122018",
        )


if __name__ == "__main__":
    unittest.main()
