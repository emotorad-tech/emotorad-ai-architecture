"""get_recent_weather through the registry (spec 2026-10-09 recent weather, section 4)."""

import json
import unittest

from emotorad_ai.agents import dealer_orders
from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS, GET_RECENT_WEATHER, build_registry
from emotorad_ai.tools.registry import ToolContext, is_error
from emotorad_ai.weather import WeatherReport, WeatherUnavailable

AREA = {"pincode": "411014", "district": "Pune", "state": "Maharashtra", "source": "location",
        "at": "2026-10-09T10:00:00+00:00"}
REPORT = WeatherReport(current_c=39.6, days=tuple(
    {"days_ago": n, "max_c": 41.0 if n < 3 else 33.0, "min_c": 26.0} for n in range(15)))


class FakeWeather:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def recent(self, pincode, point):
        self.calls.append((pincode, point))
        if self.fail:
            raise WeatherUnavailable(self.fail)
        return REPORT


def call(reg, arguments=None, area=None, said=None):
    late = {}
    if area is not None:
        late["area"] = lambda: area
    if said is not None:
        late["customer_messages"] = lambda: list(said)
    return reg.call(GET_RECENT_WEATHER, arguments or {}, ToolContext(conversation_id="c1", late=late))


class RegistrationTests(unittest.TestCase):
    def test_absent_without_a_client(self):
        self.assertNotIn(GET_RECENT_WEATHER, build_registry().specs)

    def test_never_offered_to_the_dealer_persona(self):
        self.assertNotIn(GET_RECENT_WEATHER, dealer_orders.TOOL_NAMES)

    def test_the_model_supplies_only_a_pincode(self):
        spec = build_registry(weather=FakeWeather()).specs[GET_RECENT_WEATHER]
        self.assertEqual(set(spec.parameters), {"pincode"})
        self.assertEqual(set(spec.optional_injects), {"area", "customer_messages"})

    def test_an_extra_argument_is_rejected(self):
        self.assertTrue(is_error(call(build_registry(weather=FakeWeather()), {"city": "Pune"}, AREA)))


class OutcomeTests(unittest.TestCase):
    def test_ok_is_the_summary_at_the_areas_centre(self):
        client = FakeWeather()
        data = call(build_registry(weather=client), area=AREA)["data"]
        self.assertEqual(data["outcome"], "ok")
        self.assertEqual((data["highest_c"], data["days_above_40c"]), (41, 3))
        self.assertEqual(data["area"]["pincode"], "411014")
        [(pincode, point)] = client.calls
        self.assertEqual(pincode, "411014")
        self.assertEqual(len(point), 2)
        self.assertNotIn("2026", json.dumps(data))

    def test_no_area_asks_with_the_location_button(self):
        client = FakeWeather()
        data = call(build_registry(weather=client))["data"]
        self.assertEqual(data, {"outcome": "no_area",
                                "action": {"kind": "request_location", "label": "Share my location"}})
        self.assertEqual(client.calls, [])

    def test_a_typed_pincode_wins_and_comes_back_as_the_area(self):
        data = call(build_registry(weather=FakeWeather()), {"pincode": "400054"}, AREA,
                    said=["I'm at 400054 this week"])["data"]
        self.assertEqual((data["area"]["pincode"], data["area"]["source"]), ("400054", "typed"))

    def test_a_pincode_the_customer_never_typed_or_india_post_does_not_know_is_refused(self):
        reg = build_registry(weather=FakeWeather())
        for arguments, said in (({"pincode": "110016"}, ["I am in Pune"]), ({"pincode": "999999"}, ["999999"])):
            with self.subTest(arguments=arguments):
                self.assertEqual(call(reg, arguments, AREA, said)["error"]["code"], "bad_pincode")

    def test_a_failed_lookup_is_weather_unavailable_with_its_reason(self):
        envelope = call(build_registry(weather=FakeWeather(fail="http_503")), area=AREA)
        self.assertEqual(envelope["error"]["code"], "weather_unavailable")
        self.assertIn("http_503", envelope["error"]["message"])


class SharedRuleTests(unittest.TestCase):
    def test_the_dealer_tool_still_refuses_a_pincode_never_typed(self):
        from tests.test_find_nearest_dealers import AREA as DEALER_AREA, call as dealer_call, registry

        reg, _ = registry()
        envelope = dealer_call(reg, {"pincode": "110016"}, DEALER_AREA, said=["I am in Pune"])
        self.assertEqual(envelope["error"]["code"], "bad_pincode")
        self.assertIn(FIND_NEAREST_DEALERS, reg.specs)


if __name__ == "__main__":
    unittest.main()
