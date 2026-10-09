"""The API wires the weather (spec 2026-10-09 recent weather, section 7)."""

import unittest

from emotorad_ai.tools.mocks import GET_RECENT_WEATHER
from tests.test_api_health import fresh_api, zoho_blank


class WiringTests(unittest.TestCase):
    def tearDown(self):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": ""}, **zoho_blank()))

    def test_without_the_key_the_tool_is_absent(self):
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": ""}, **zoho_blank()))
        self.assertNotIn(GET_RECENT_WEATHER, api.registry.specs)
        self.assertEqual(api.health()["weather"], "not configured")

    def test_with_the_key_the_tool_is_there(self):
        api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_OPEN_METEO_API_KEY": "test-key-not-real"},
                             **zoho_blank()))
        self.assertIn(GET_RECENT_WEATHER, api.registry.specs)
        self.assertEqual(api.health()["weather"], "open-meteo")
        self.assertNotIn("test-key-not-real", str(api.health()))


if __name__ == "__main__":
    unittest.main()
