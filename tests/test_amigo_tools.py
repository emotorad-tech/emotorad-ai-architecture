"""The two Amigo tools the battery and motor agents may call (spec section 3)."""

import unittest

from emotorad_ai.agents import battery_support, motor_support
from emotorad_ai.tools.mocks import GET_RECENT_TRIPS, GET_SERVICE_STATUS, build_registry
from emotorad_ai.tools.registry import ToolContext
from tests.amigo_fake import RIDER_A, RIDER_C, FakeAmigo


def call(reg, name, phone):
    return reg.call(name, {}, ToolContext(conversation_id="c1", phone=phone))


class ServiceStatusTests(unittest.TestCase):
    def test_rider_c(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_SERVICE_STATUS, RIDER_C)["data"]
        self.assertEqual(data["bike"], "T-Rex Air")
        self.assertEqual(data["odometer_km"], 1180)
        self.assertEqual(data["stages"], [
            {"stage": "250 km / 1 month", "status": "done"},
            {"stage": "1000 km / 6 months", "status": "due"},
            {"stage": "2000 km / 12 months", "status": "upcoming"},
        ])

    def test_no_record(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_SERVICE_STATUS, RIDER_A)["data"]
        self.assertIsNone(data["stages"])
        self.assertIn("No service record", data["note"])

    def test_amigo_down(self):
        envelope = call(build_registry(amigo=FakeAmigo(down=True)), GET_SERVICE_STATUS, RIDER_C)
        self.assertEqual(envelope["error"]["code"], "amigo_unavailable")
        self.assertTrue(envelope["error"]["retryable"])


class RecentTripsTests(unittest.TestCase):
    def test_rider_c_newest_first_in_ist(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_RECENT_TRIPS, RIDER_C)["data"]
        first = data["trips"][0]
        self.assertEqual(first["when"], "29 Sep 2026, 07:40")
        self.assertEqual(first["bike"], "T-Rex Air")
        self.assertEqual(first["distance_km"], 12.6)
        self.assertEqual(first["duration_min"], 42)
        self.assertEqual(first["average_kmh"], 18.0)
        self.assertEqual(len(data["trips"]), 3)

    def test_no_rides(self):
        data = call(build_registry(amigo=FakeAmigo()), GET_RECENT_TRIPS, RIDER_A)["data"]
        self.assertEqual(data["trips"], [])
        self.assertIn("No rides", data["note"])

    def test_nothing_identifying_in_the_output(self):
        reg = build_registry(amigo=FakeAmigo())
        said = repr(call(reg, GET_RECENT_TRIPS, RIDER_C)) + repr(call(reg, GET_SERVICE_STATUS, RIDER_C))
        for bit in ("TESTVIN", "TESTTREX", "9700000033", "emuserid"):
            self.assertNotIn(bit, said)


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_reader(self):
        reg = build_registry()
        self.assertNotIn(GET_SERVICE_STATUS, reg.specs)
        self.assertNotIn(GET_RECENT_TRIPS, reg.specs)

    def test_the_battery_and_motor_agents_list_them(self):
        for agent in (battery_support, motor_support):
            self.assertIn(GET_SERVICE_STATUS, agent.TOOL_NAMES)
            self.assertIn(GET_RECENT_TRIPS, agent.TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
