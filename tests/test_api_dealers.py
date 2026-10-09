"""The API wires the dealer stores (spec 2026-10-09)."""

import unittest

from fastapi.testclient import TestClient

from emotorad_ai.tools.mocks import FIND_NEAREST_DEALERS
from tests.test_prepare_turn import fresh_api


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api()

    def test_the_tool_is_registered_and_the_runtime_takes_its_cards(self):
        self.assertIn(FIND_NEAREST_DEALERS, self.api.registry.specs)
        self.assertIs(self.api.runtime.store_cards, self.api.STORE_CARDS)

    def test_health_names_the_source(self):
        body = TestClient(self.api.app).get("/health").json()
        self.assertEqual(body["dealer_stores"], "fixtures")


if __name__ == "__main__":
    unittest.main()
