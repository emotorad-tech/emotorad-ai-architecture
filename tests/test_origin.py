"""IP and phone to a place, for reporting where conversations come from."""

import logging
import os
import unittest

from emotorad_ai.origin import (UNKNOWN, IpLocator, Place, choose, from_phone, ip_locator_from_env,
                                place_from_dict)

PUNE = {"country": {"iso_code": "IN", "names": {"en": "India"}},
        "subdivisions": [{"names": {"en": "Maharashtra"}}], "city": {"names": {"en": "Pune"}}}


class FakeReader:
    def __init__(self, records=None, fail=None):
        self.records, self.fail, self.asked = records or {}, fail, []

    def get(self, ip):
        self.asked.append(ip)
        if self.fail:
            raise self.fail
        return self.records.get(ip)


class IpTests(unittest.TestCase):
    def locator(self, **kw):
        return IpLocator(FakeReader(**kw), "dbip-city-lite-2026-10")

    def test_a_dbip_record_becomes_a_place(self):
        place = self.locator(records={"49.36.1.1": PUNE}).place("49.36.1.1")
        self.assertEqual(place, Place("IN", "Maharashtra", "Pune", "ip", "dbip-city-lite-2026-10"))

    def test_an_ipv4_mapped_ipv6_address_is_looked_up_as_ipv4(self):
        locator = self.locator(records={"49.36.1.1": PUNE})
        self.assertEqual(locator.place("::ffff:49.36.1.1").city, "Pune")

    def test_private_loopback_and_junk_are_not_looked_up(self):
        locator = self.locator(records={"49.36.1.1": PUNE})
        for ip in ("10.1.2.3", "127.0.0.1", "172.17.0.1", "::1", "testclient", "", None):
            self.assertIsNone(locator.place(ip))
        self.assertEqual(locator._reader.asked, [])

    def test_an_address_not_in_the_file_is_none(self):
        self.assertIsNone(self.locator().place("49.36.1.1"))

    def test_a_record_with_only_a_country(self):
        place = self.locator(records={"49.36.1.1": {"country": {"iso_code": "ES"}}}).place("49.36.1.1")
        self.assertEqual((place.country, place.region, place.city), ("ES", None, None))

    def test_a_read_error_is_logged_by_class_and_gives_none(self):
        locator = self.locator(fail=OSError("49.36.1.1 broke"))
        with self.assertLogs("emotorad_ai.origin", level="WARNING") as logs:
            self.assertIsNone(locator.place("49.36.1.1"))
        self.assertIn("origin_lookup_failed (OSError)", logs.output[0])
        self.assertNotIn("49.36.1.1", " ".join(logs.output))


class PhoneTests(unittest.TestCase):
    def test_india_and_spain_by_calling_code(self):
        self.assertEqual(from_phone("+919700000031"), Place("IN", None, None, "phone", None))
        self.assertEqual(from_phone("+34612345678").country, "ES")

    def test_another_code_or_no_phone_is_none(self):
        for phone in ("+447700900123", "9700000031", "", None):
            self.assertIsNone(from_phone(phone))


class ChooseTests(unittest.TestCase):
    def test_the_first_resolved_place_wins(self):
        ip = Place("IN", "Maharashtra", "Pune", "ip", "db")
        self.assertEqual(choose(None, ip, from_phone("+34612345678")), ip)

    def test_nothing_resolved_is_unknown(self):
        self.assertEqual(choose(None, None), UNKNOWN)
        self.assertEqual(UNKNOWN.as_dict(),
                         {"country": "unknown", "region": None, "city": None, "source": "none", "db": None})

    def test_a_place_survives_the_round_trip_through_entry_metadata(self):
        place = Place("IN", "Maharashtra", "Pune", "ip", "db")
        self.assertEqual(place_from_dict(place.as_dict()), place)
        self.assertIsNone(place_from_dict(None))
        self.assertIsNone(place_from_dict({"city": "Pune"}))


class FileTests(unittest.TestCase):
    def test_no_file_means_no_locator(self):
        self.assertIsNone(ip_locator_from_env({"EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"}))

    def test_an_unreadable_file_is_logged_once_and_gives_no_locator(self):
        path = os.path.join(os.path.dirname(__file__), "test_origin.py")  # exists, not an mmdb
        with self.assertLogs("emotorad_ai.origin", level="WARNING") as logs:
            self.assertIsNone(ip_locator_from_env({"EMOTORAD_GEO_DB": path}))
        self.assertIn("origin_db_unavailable", logs.output[0])


@unittest.skipUnless(os.environ.get("EMOTORAD_TEST_GEO_DB"), "needs a real DB-IP .mmdb file")
class RealFileTests(unittest.TestCase):
    def test_a_known_public_address_resolves(self):
        locator = ip_locator_from_env({"EMOTORAD_GEO_DB": os.environ["EMOTORAD_TEST_GEO_DB"]})
        self.assertTrue(locator.db.startswith("dbip-city-lite-"))
        self.assertEqual(locator.place("8.8.8.8").country, "US")


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
