"""Dealer stores from OMS, placed at their pin codes' centres (spec 2026-10-09, section 2)."""

import unittest

from emotorad_ai.geo import PincodeCentres
from emotorad_ai.tools import dealer_stores
from emotorad_ai.tools.dealer_stores import (
    DealerDirectory, DealerStoresUnavailable, StoreCards, directory_from_env, display_phone, full_address,
    store_card, store_from_row,
)

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "400054": (19.06, 72.84), "110016": (28.55, 77.20)})


def row(**fields):
    base = {"store_name": "Test Cycles", "address": "Shop 1, Test Road", "address2": None, "pin_code": "411014",
            "district_name": "Pune", "state_name": "Maharashtra", "manager_name": "Test Manager",
            "manager_mobile": "9000000001", "poc_name": "Test Owner", "store_mobile": "9000000009"}
    base.update(fields)
    return base


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RowTests(unittest.TestCase):
    def test_the_franchise_manager_is_the_contact(self):
        store = store_from_row(row(), CENTRES)
        self.assertEqual((store.manager_name, store.phone), ("Test Manager", "+91 9000000001"))
        self.assertEqual(store.point, (18.56, 73.91))

    def test_without_a_manager_the_stores_contact_person(self):
        store = store_from_row(row(manager_name=None, manager_mobile=None), CENTRES)
        self.assertEqual((store.manager_name, store.phone), ("Test Owner", "+91 9000000009"))

    def test_a_store_with_no_contact_or_no_centre_is_left_out(self):
        self.assertIsNone(store_from_row(row(manager_name=None, manager_mobile=None, poc_name="", store_mobile=""), CENTRES))
        self.assertIsNone(store_from_row(row(pin_code="999999"), CENTRES))

    def test_phones_are_shown_with_the_country_code(self):
        self.assertEqual(display_phone("09000000001"), "+91 9000000001")
        self.assertEqual(display_phone("+91 90000-00001"), "+91 9000000001")
        self.assertEqual(display_phone("919000000001"), "+91 9000000001")
        self.assertEqual(display_phone(" 020 1234 "), "020 1234")

    def test_the_address_joins_its_parts_once(self):
        self.assertEqual(full_address(row(address2="Near Test Chowk")),
                         "Shop 1, Test Road, Near Test Chowk, Pune, Maharashtra - 411014")
        self.assertEqual(full_address(row(address="Shop 1, Pune 411014", address2="pune")),
                         "Shop 1, Pune 411014, pune, Maharashtra")

    def test_the_card_shape(self):
        store = store_from_row(row(), CENTRES)
        self.assertEqual(store_card("D1", store, 3), {
            "ref": "D1", "name": "Test Cycles", "address": "Shop 1, Test Road, Pune, Maharashtra - 411014",
            "pincode": "411014", "manager_name": "Test Manager", "phone": "+91 9000000001", "distance_km": 3})


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.rows = [row(store_name="Pune Store"), row(store_name="Mumbai Store", pin_code="400054"),
                     row(store_name="Delhi Store", pin_code="110016"), row(store_name="Nowhere", pin_code="999999")]
        self.fail = False
        self.clock = Clock()
        self.events = []

        def load():
            self.calls += 1
            if self.fail:
                raise OSError("down")
            return [dict(r) for r in self.rows]

        self.directory = DealerDirectory(load, CENTRES, clock=self.clock,
                                         log=lambda event, fields: self.events.append((event, fields)))

    def test_nearest_three_in_order_with_whole_kilometres(self):
        found = self.directory.nearest((18.52, 73.86))
        self.assertEqual([s.name for s, _ in found], ["Pune Store", "Mumbai Store", "Delhi Store"])
        self.assertEqual(found[0][1], 7)
        self.assertTrue(all(isinstance(km, int) for _, km in found))

    def test_ties_are_broken_by_name(self):
        self.rows = [row(store_name="B Store"), row(store_name="A Store")]
        self.assertEqual([s.name for s, _ in self.directory.nearest((18.56, 73.91))], ["A Store", "B Store"])

    def test_loaded_once_for_ten_minutes_and_unplaced_counted_without_names(self):
        self.directory.stores()
        self.clock.now += dealer_stores.CACHE_SECONDS - 1
        self.directory.stores()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.events, [("dealer_stores_loaded", {"stores": 3, "unplaced": 1})])
        self.clock.now += 2
        self.directory.stores()
        self.assertEqual(self.calls, 2)

    def test_a_failed_refresh_serves_the_last_list_for_an_hour(self):
        self.directory.stores()
        self.fail = True
        self.clock.now += dealer_stores.CACHE_SECONDS + 1
        self.assertEqual(len(self.directory.stores()), 3)
        self.clock.now += dealer_stores.STALE_SECONDS
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()

    def test_a_failed_first_load_is_unavailable_and_the_breaker_holds_a_minute(self):
        self.fail = True
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()
        with self.assertRaises(DealerStoresUnavailable):
            self.directory.stores()
        self.assertEqual(self.calls, 1)
        self.clock.now += dealer_stores.FAILURE_TTL_SECONDS + 1
        self.fail = False
        self.assertEqual(len(self.directory.stores()), 3)

    def test_a_failed_load_is_logged_with_its_class_and_whether_stale_is_served(self):
        """The final review's I4: never swallowed silently."""
        self.directory.stores()
        self.fail = True
        self.clock.now += dealer_stores.CACHE_SECONDS + 1
        self.directory.stores()
        self.assertEqual(self.events[-1], ("dealer_stores_load_failed", {"error": "OSError", "serving_stale": True}))

    def test_the_error_names_no_connection_detail(self):
        def load():
            raise OSError("postgresql://user:secret@host/db")

        with self.assertRaises(DealerStoresUnavailable) as caught:
            DealerDirectory(load, CENTRES).stores()
        self.assertNotIn("secret", str(caught.exception))


class EnvTests(unittest.TestCase):
    def test_without_a_dsn_outside_offline_mode_there_is_no_directory(self):
        """The final review's I3: made-up stores never reach a real rider."""
        directory, source = directory_from_env(CENTRES, environ={}, offline=False)
        self.assertIsNone(directory)
        self.assertEqual(source, "not configured")

    def test_without_a_dsn_the_fixtures(self):
        directory, source = directory_from_env(CENTRES, environ={}, offline=True)
        self.assertEqual(source, "fixtures")
        self.assertEqual(len(directory.stores()), 3)

    def test_with_a_dsn_the_database(self):
        _, source = directory_from_env(CENTRES, environ={dealer_stores.DSN_ENV: "postgresql://x"})
        self.assertEqual(source, "oms_db")


class CardsTests(unittest.TestCase):
    def test_taken_once(self):
        cards = StoreCards()
        cards.put("c1", [{"ref": "D1"}])
        self.assertEqual(cards.take("c1"), [{"ref": "D1"}])
        self.assertEqual(cards.take("c1"), [])
        self.assertEqual(cards.take("c2"), [])


if __name__ == "__main__":
    unittest.main()
