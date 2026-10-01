"""The Amiigo app's "Delete my conversation data" button (spec 2026-10-01)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai import erasure
from emotorad_ai.conversation import StoreUnavailable
from emotorad_ai.tools.fixtures import PHONE_AMIIGO_TEST_RIDER
from tests.test_api_health import fresh_api

RIDER_KEY = "PHONE#" + PHONE_AMIIGO_TEST_RIDER


class ErasureEndpointTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.client = TestClient(self.api.app)

    def post(self, path, **body):
        return self.client.post(path, json=dict({"session_token": "sess-amiigo-test"}, **body))

    def test_a_signed_in_rider_asks_once(self):
        first = self.post("/erasure-requests", confirm=True)
        self.assertEqual(first.status_code, 201, first.text)
        reference = first.json()["reference"]
        self.assertEqual(first.json(), {"reference": reference, "status": "pending",
                                        "text": erasure.ERASURE_REQUESTED.format(reference=reference)})
        again = self.post("/erasure-requests", confirm=True)
        self.assertEqual((again.status_code, again.json()["reference"]), (200, reference))
        self.assertEqual(again.json()["text"], erasure.ERASURE_EXISTING.format(reference=reference))

    def test_without_confirm_nothing_is_recorded(self):
        self.assertEqual(self.post("/erasure-requests").status_code, 400)
        self.assertIsNone(self.api.stores.conversations.pending_erasure_of(RIDER_KEY))

    def test_an_unknown_session_is_refused(self):
        for token in ("sess-nobody", ""):
            r = self.client.post("/erasure-requests", json={"session_token": token, "confirm": True})
            self.assertEqual((r.status_code, r.json()["detail"]), (403, erasure.ERASURE_SIGN_IN))

    def test_status_then_cancel_then_nothing_to_cancel(self):
        self.assertEqual(self.post("/erasure-requests/status").json(), {"reference": None, "status": "none"})
        reference = self.post("/erasure-requests", confirm=True).json()["reference"]
        status = self.post("/erasure-requests/status").json()
        self.assertEqual((status["reference"], status["status"]), (reference, "pending"))
        self.assertIn("requested_at", status)
        cancelled = self.post("/erasure-requests/cancel")
        self.assertEqual(cancelled.json(), {"reference": reference, "status": "cancelled",
                                            "text": erasure.ERASURE_CANCELLED.format(reference=reference)})
        nothing = self.post("/erasure-requests/cancel")
        self.assertEqual((nothing.status_code, nothing.json()["detail"]), (404, erasure.ERASURE_NOTHING_TO_CANCEL))

    def test_a_request_made_in_the_chat_is_the_one_the_app_sees(self):
        reference = self.api.stores.conversations.request_erasure(RIDER_KEY, "website_chat", "c9",
                                                                  "2026-10-01T09:00:00+00:00")
        self.assertEqual(self.post("/erasure-requests/status").json()["reference"], reference)
        self.assertEqual(self.post("/erasure-requests", confirm=True).json()["reference"], reference)

    def test_a_store_failure_is_503_and_logged_without_the_token(self):
        with mock.patch.object(self.api.stores.conversations, "pending_erasure_of",
                               side_effect=StoreUnavailable("down")):
            r = self.post("/erasure-requests", confirm=True)
        self.assertEqual((r.status_code, r.json()["detail"]), (503, erasure.ERASURE_FAILED))
        (event,) = [e for e in self.api.log.events if e["event"] == "erasure_request_failed"]
        self.assertEqual(event["error"], "StoreUnavailable")
        self.assertNotIn("sess-amiigo-test", repr(self.api.log.events))
