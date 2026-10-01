"""Every photo gets a safety look on arrival (spec 2026-10-01, revised)."""

import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.photo_check import PhotoCheckError
from tests.test_api_media_persistence import _Store, fresh_api, jpeg_data_url


class FakeChecker:
    """Answers each photo, in order, with a list of live hazards."""

    provider = "openrouter"

    def __init__(self, answers=(["swelling", "smoke"],), error=None, delay=0.0):
        self.answers, self.error, self.delay, self.seen = list(answers), error, delay, []

    def check(self, data, mime):
        self.seen.append((len(data), mime))
        n = len(self.seen)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answers[min(n, len(self.answers)) - 1]


def photo():
    return {"kind": "image", "url": jpeg_data_url()}


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
        reply = self.post(FakeChecker(), [photo()])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertTrue(reply["ticket_id"])

    def test_a_photo_with_no_live_hazard_is_an_ordinary_turn(self):
        reply = self.post(FakeChecker(answers=([],)), [photo()])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_one_hazard_among_three_photos_is_enough(self):
        reply = self.post(FakeChecker(answers=([], ["sparks"], [])), [photo(), photo(), photo()])
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")

    def test_three_slow_checks_run_together(self):
        started = time.monotonic()
        self.post(FakeChecker(answers=([],), delay=0.5), [photo(), photo(), photo()])
        self.assertLess(time.monotonic() - started, 1.3)  # one after another would be 1.5 s

    def test_a_check_past_the_deadline_is_skipped_and_logged(self):
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.2):
            started = time.monotonic()
            reply = self.post(FakeChecker(delay=2.0), [photo()])
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")
        (event,) = [e for e in self.api.log.events if e["event"] == "photo_check_skipped"]
        self.assertEqual(event["error"], "timeout")

    def test_a_failing_check_logs_its_code_and_the_photo_goes_on(self):
        reply = self.post(FakeChecker(error=PhotoCheckError("bad_json")), [photo()])
        self.assertNotEqual(reply["handled_by"], "guardrail:battery_safety")
        (event,) = [e for e in self.api.log.events if e["event"] == "photo_check_skipped"]
        self.assertEqual(event["error"], "bad_json")

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
        reply = self.post(FakeChecker(), [photo()], session=None)
        self.assertEqual(reply["handled_by"], "guardrail:battery_safety")
        self.assertFalse(reply["ticket_id"])

    def test_the_photo_stays_in_history_after_the_safety_turn(self):
        # The final review: the safety branch rebuilt the turn without fetching,
        # so a stored photo became "could not be retrieved" for every later turn.
        self.post(FakeChecker(), [photo()])
        state = self.api.stores.conversations.get("c1")
        user_turns = [m for m in state.history if m["role"] == "user"]
        blocks = user_turns[-1]["content"]
        self.assertTrue(any(isinstance(b, dict) and b.get("type") == "image" for b in blocks), blocks)
        self.assertTrue(state.evidence_seen)

    def test_hazards_are_logged_only_with_dev_codes(self):
        with mock.patch.object(self.api, "DEV_CODES", False):
            self.post(FakeChecker(answers=([],)), [photo()])
        self.assertEqual([e for e in self.api.log.events if e["event"] == "photo_check"], [])


class HealthTests(unittest.TestCase):
    def test_health_says_whether_photos_are_checked(self):
        api = fresh_api(None)
        self.assertEqual(api.health()["photo_check"], "off")
        with mock.patch.object(api, "PHOTO_CHECKER", FakeChecker()):
            self.assertEqual(api.health()["photo_check"], "openrouter")
