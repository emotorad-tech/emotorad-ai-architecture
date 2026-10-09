"""find_nearest_dealers through the registry (spec 2026-10-09, section 4)."""

import json
import os
import unittest
from unittest import mock

from emotorad_ai.agents import dealer_orders
from emotorad_ai.geo import PincodeCentres
from emotorad_ai.tools.dealer_stores import FIXTURE_ROWS, DealerDirectory, StoreCards
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error

CENTRES = PincodeCentres({"411014": (18.56, 73.91), "411001": (18.52, 73.86), "400054": (19.06, 72.84),
                          "110016": (28.55, 77.20), "560001": (12.97, 77.59)})
AREA = {"pincode": "411001", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}


def registry(load=None, cards=None):
    directory = DealerDirectory(load or (lambda: [dict(r) for r in FIXTURE_ROWS]), CENTRES)
    return build_registry(dealers=directory, store_cards=cards if cards is not None else StoreCards()), cards


def call(reg, arguments=None, area=None):
    late = {"area": (lambda: area)} if area is not None else {}
    return reg.call(FIND_NEAREST_DEALERS, arguments or {}, ToolContext(conversation_id="c1", late=late))


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_directory(self):
        self.assertNotIn(FIND_NEAREST_DEALERS, build_registry().specs)

    def test_never_offered_to_the_dealer_persona(self):
        self.assertNotIn(FIND_NEAREST_DEALERS, dealer_orders.TOOL_NAMES)

    def test_the_model_cannot_supply_the_area(self):
        reg, _ = registry()
        self.assertEqual(set(reg.specs[FIND_NEAREST_DEALERS].parameters), {"pincode"})
        self.assertIn("area", reg.specs[FIND_NEAREST_DEALERS].optional_injects)

    def test_an_extra_argument_is_rejected(self):
        reg, _ = registry()
        envelope = call(reg, {"pincode": "411001", "phone": "9999999999"}, AREA)
        self.assertTrue(is_error(envelope))


class OutcomeTests(unittest.TestCase):
    def test_ok_gives_the_model_names_and_distances_and_code_the_cards(self):
        cards = StoreCards()
        reg, _ = registry(cards=cards)
        envelope = call(reg, area=AREA)
        data = envelope["data"]
        self.assertEqual(data["outcome"], "ok")
        self.assertEqual([s["store_ref"] for s in data["stores"]], ["D1", "D2", "D3"])
        self.assertEqual(data["stores"][0]["store_name"], "Test Cycles Pune")
        self.assertEqual(data["stores"][0]["locality"], "Pune, Maharashtra")
        self.assertFalse(data["far"])
        seen = json.dumps(envelope)
        for hidden in ("9000000001", "Test Manager One", "Shop 1, Test Road"):
            self.assertNotIn(hidden, seen)
        taken = cards.take("c1")
        self.assertEqual([c["ref"] for c in taken], ["D1", "D2", "D3"])
        self.assertEqual((taken[0]["manager_name"], taken[0]["phone"]), ("Test Manager One", "+91 9000000001"))

    def test_a_typed_pincode_wins_and_is_returned_as_the_area(self):
        reg, _ = registry()
        data = call(reg, {"pincode": " 400054 "}, AREA)["data"]
        self.assertEqual(data["stores"][0]["store_name"], "Test Cycles Mumbai")
        self.assertEqual((data["area"]["pincode"], data["area"]["source"]), ("400054", "typed"))

    def test_no_area_asks_with_the_location_button(self):
        reg, _ = registry()
        data = call(reg)["data"]
        self.assertEqual(data, {"outcome": "no_area",
                                "action": {"kind": "request_location", "label": "Share my location"}})

    def test_a_pincode_india_post_does_not_know_is_bad_pincode(self):
        reg, _ = registry()
        for typed in ("999999", "12345", "abcdef", "011001"):
            with self.subTest(typed=typed):
                envelope = call(reg, {"pincode": typed}, AREA)
                self.assertEqual(envelope["error"]["code"], "bad_pincode")

    def test_unreadable_stores_are_oms_unavailable_with_the_care_contact(self):
        def down():
            raise OSError("down")

        reg, _ = registry(load=down)
        with mock.patch.dict(os.environ, {"EMOTORAD_CUSTOMER_CARE_CONTACT": "1800 000 0000"}):
            envelope = call(reg, area=AREA)
        self.assertEqual(envelope["error"]["code"], "oms_unavailable")
        self.assertIn("1800 000 0000", envelope["error"]["message"])

    def test_far_when_the_nearest_is_over_100_km(self):
        reg, _ = registry()
        far_area = dict(AREA, pincode="560001", district="Bengaluru", state="Karnataka")
        with mock.patch.dict(os.environ, {"EMOTORAD_CUSTOMER_CARE_CONTACT": "1800 000 0000"}):
            data = call(reg, area=far_area)["data"]
        self.assertTrue(data["far"])
        self.assertEqual(data["care_contact"], "1800 000 0000")


if __name__ == "__main__":
    unittest.main()
