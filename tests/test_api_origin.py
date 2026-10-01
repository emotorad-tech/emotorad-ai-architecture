"""The API turns the customer's IP into a place and passes only the place."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply
from emotorad_ai.origin import Place
from tests.test_api_health import fresh_api


class FakeLocator:
    db = "dbip-city-lite-2026-10"

    def __init__(self):
        self.seen = []

    def place(self, ip):
        self.seen.append(ip)
        return Place("IN", "Maharashtra", "Pune", "ip", self.db)


class ApiOriginTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.client = TestClient(self.api.app)

    def post(self, locator, headers):
        seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        with mock.patch.object(self.api, "IP_LOCATOR", locator), \
                mock.patch.object(self.api, "TRUSTED_PROXIES", frozenset({"testclient"})), \
                mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: seen.append(m) or scripted):
            r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                                   "text": "hi"}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return seen[-1]

    def test_the_place_goes_in_and_the_ip_does_not(self):
        locator = FakeLocator()
        message = self.post(locator, {"X-Real-IP": "49.36.1.1"})
        self.assertEqual(message.entry_metadata["origin"]["city"], "Pune")
        self.assertEqual(locator.seen, ["49.36.1.1"])
        self.assertNotIn("49.36.1.1", repr(message.to_dict()))

    def test_without_a_file_no_origin_is_passed(self):
        message = self.post(None, {"X-Real-IP": "49.36.1.1"})
        self.assertNotIn("origin", message.entry_metadata)

    def test_health_names_the_file_in_use(self):
        # A reload changes the one module object, so the no-file answer first.
        self.assertEqual(self.api.health()["ip_location"], "not configured")
        with mock.patch("emotorad_ai.origin.ip_locator_from_env", return_value=FakeLocator()):
            api = fresh_api({"EMOTORAD_AI_MODE": "offline"})
        self.assertEqual(api.health()["ip_location"], "dbip-city-lite-2026-10")

    @classmethod
    def tearDownClass(cls):
        fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
