"""The API's warranty step switch (spec 2026-10-09 warranty step)."""

import unittest

from tests.test_api_health import fresh_api, zoho_blank


class SwitchTests(unittest.TestCase):
    def tearDown(self):
        fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_WARRANTY_STEP": ""}, **zoho_blank()))

    def test_off_unless_exactly_on(self):
        for value, expected in (("", "off"), ("yes", "off"), ("on", "on")):
            with self.subTest(value=value):
                api = fresh_api(dict({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_WARRANTY_STEP": value},
                                     **zoho_blank()))
                self.assertEqual(api.health()["warranty_step"], expected)
                self.assertEqual(api.runtime.warranty_step, expected == "on")


if __name__ == "__main__":
    unittest.main()
