"""Every photo gets a safety look on arrival (spec 2026-10-01)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_api_media_persistence import _Store, fresh_api, jpeg_data_url

HAZARD = "The battery casing is swollen and white smoke rises from it."
CLEAR = ("A battery pack on a table. No smoke visible, no flames visible, no scorch or burn marks visible, "
         "no swelling or bulging visible, no melting visible, no leaking fluid visible, no sparks visible.")


class FakeChecker:
    provider = "openrouter"

    def __init__(self, notes=(HAZARD,), error=None):
        self.notes, self.error, self.seen = list(notes), error, []

    def check(self, data, mime):
        self.seen.append((len(data), mime))
        if self.error is not None:
            raise self.error
        return self.notes[min(len(self.seen), len(self.notes)) - 1]


class PhotoCheckTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)
        self.addCleanup(lambda: fresh_api(None))

    def post(self, checker, attachments, text="this is what i see", session="sess-ananya"):
        body = {"conversation_id": "c1", "text": text, "attachments": attachments}
        if session:
            body["session_token"] = session
        with mock.patch.object(self.api, "PHOTO_CHECKER", checker):
            r = self.client.post("/message", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_hazard_photo_gets_the_safety_reply_and_a_ticket_on_that_turn(self):
        reply = self.post(FakeChecker(), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertTrue(reply["ticket_id"])

    def test_a_clear_photo_goes_on_as_usual(self):
        reply = self.post(FakeChecker(notes=(CLEAR,)), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_one_hazard_among_three_photos_is_enough(self):
        photo = {"kind": "image", "url": jpeg_data_url()}
        reply = self.post(FakeChecker(notes=(CLEAR, HAZARD, CLEAR)), [dict(photo), dict(photo), dict(photo)])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_a_failing_check_leaves_the_photo_and_is_logged(self):
        reply = self.post(FakeChecker(error=TimeoutError("slow")), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")
        (event,) = [e for e in self.api.log.events if e["event"] == "photo_check_skipped"]
        self.assertEqual(event["error"], "TimeoutError")

    def test_an_uploaded_photo_is_checked_too(self):
        slot = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1",
                                                  "tree": "customers", "mime_type": "image/jpeg",
                                                  "size_bytes": 100}).json()
        self.store.objects[slot["key"]] = b"x" * 100
        self.store.meta[slot["key"]] = {"size": 100, "mime": "image/jpeg"}
        checker = FakeChecker()
        reply = self.post(checker, [{"upload_id": slot["upload_id"]}], text="")
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertEqual(checker.seen, [(100, "image/jpeg")])

    def test_an_unverified_visitor_gets_the_safety_reply_without_a_ticket(self):
        reply = self.post(FakeChecker(), [{"kind": "image", "url": jpeg_data_url()}], session=None)
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertFalse(reply["ticket_id"])

    def test_the_description_is_logged_only_with_dev_codes(self):
        with mock.patch.object(self.api, "DEV_CODES", False):
            self.post(FakeChecker(notes=(CLEAR,)), [{"kind": "image", "url": jpeg_data_url()}])
        self.assertEqual([e for e in self.api.log.events if e["event"] == "photo_check"], [])


class HealthTests(unittest.TestCase):
    def test_health_says_whether_photos_are_checked(self):
        api = fresh_api(None)
        self.assertEqual(api.health()["photo_check"], "off")
        with mock.patch.object(api, "PHOTO_CHECKER", FakeChecker()):
            self.assertEqual(api.health()["photo_check"], "openrouter")
