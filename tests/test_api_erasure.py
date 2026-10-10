"""The Amiigo app's "Delete my conversation data" button (spec 2026-10-01)."""

import unittest
from datetime import datetime
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


    def test_the_request_records_the_app_sign_in(self):
        reference = self.post("/erasure-requests", confirm=True).json()["reference"]
        self.assertEqual(self.api.stores.conversations.erasure_record(reference)["proof"], {"method": "app_sign_in"})


class WebsiteChatButtonTests(unittest.TestCase):
    """The HTML chat's delete button: the visitor proved their number in the
    chat, so the conversation that verified is the identity (2026-10-01)."""

    def setUp(self):
        self.api = fresh_api({"EMOTORAD_AI_MODE": "offline", "EMOTORAD_GEO_DB": "C:/nowhere/none.mmdb"})
        self.client = TestClient(self.api.app)

    def verify(self, conversation_id, phone="+919700000033"):
        self.api.verification_store.issue(conversation_id, phone, "123456")
        self.assertTrue(self.api.verification_store.check(conversation_id, "123456"))

    def test_a_verified_chat_records_a_website_request(self):
        self.verify("web-1")
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-1", "confirm": True})
        self.assertEqual(r.status_code, 201, r.text)
        record = self.api.stores.conversations.pending_erasure_of("PHONE#+919700000033")
        self.assertEqual((record["_id"], record["channel"]), (r.json()["reference"], "website_chat"))

    def test_an_unverified_chat_is_told_to_verify_first(self):
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-2", "confirm": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, erasure.ERASURE_VERIFY_FIRST))
        r = self.client.post("/erasure-requests", json={"confirm": True})
        self.assertEqual(r.status_code, 403)

    def test_status_and_cancel_by_the_verified_chat(self):
        self.verify("web-3")
        reference = self.client.post("/erasure-requests", json={"conversation_id": "web-3", "confirm": True}).json()["reference"]
        self.assertEqual(self.client.post("/erasure-requests/status", json={"conversation_id": "web-3"}).json()["reference"],
                         reference)
        cancelled = self.client.post("/erasure-requests/cancel", json={"conversation_id": "web-3"})
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def restarted(self, conversation_id, verify_step=None):
        """Verified, the saved conversation as `verify_step` leaves it, then
        a new process: nothing in memory, the same saved sessions."""
        from emotorad_ai.tools.verification import VerificationStore

        self.verify(conversation_id)
        state = self.api.stores.conversations.get(conversation_id)
        state.user_key, state.verify_step = "PHONE#+919700000033", verify_step
        self.api.stores.conversations.save(state)
        self.api.verification_store = VerificationStore(sessions=self.api.stores.verified_sessions, log=self.api.log)

    def test_after_a_restart_the_verified_chat_can_still_ask(self):
        self.restarted("web-5")
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-5", "confirm": True})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.api.stores.conversations.erasure_record(r.json()["reference"])["proof"]["method"], "otp")

    def test_after_a_restart_a_chat_that_changed_its_number_cannot(self):
        # The saved conversation says the number is being changed: the saved
        # session it no longer agrees with proves nothing (the review, minor 3).
        self.restarted("web-6", verify_step="number")
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-6", "confirm": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, erasure.ERASURE_VERIFY_FIRST))

    def test_the_request_records_the_otp_and_when(self):
        self.verify("web-3")
        r = self.client.post("/erasure-requests", json={"conversation_id": "web-3", "confirm": True})
        proof = self.api.stores.conversations.erasure_record(r.json()["reference"])["proof"]
        self.assertEqual(proof["method"], "otp")
        self.assertIsNotNone(datetime.fromisoformat(proof["verified_at"]))
