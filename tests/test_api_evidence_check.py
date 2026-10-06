"""The evidence check at ingest (api._inbound_attachments, the person's brief
of 6 October 2026, item 2).

In a fault chat with the switch on, a turn's photos and videos go to the
checker beside the photo safety check and the video summary, under the same
deadline, and the verdict reaches the turn in entry_metadata the way
`photos_unchecked` does. Nothing is written outside the turn. The checker is
faked; no network, no AWS.
"""

import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply
from emotorad_ai.evidence_check import INLINE_LIMIT, EvidenceCheckError, EvidenceVerdict
from tests.test_api_media_persistence import jpeg_data_url
from tests.test_api_photo_check import FakeChecker as FakePhotoChecker
from tests.test_api_uploads import _Store, _Summariser, fresh_api

PASS = EvidenceVerdict(shows_part=True, fault_visible=True, matches_complaint=True,
                       seen="The charger light stays red.", missing="")
FAIL = EvidenceVerdict(shows_part=True, fault_visible=False, matches_complaint=False,
                       seen="SECRET-SEEN the pack on a table.", missing="SECRET-MISSING the charger plugged in.")


class FakeEvidenceChecker:
    provider = "openrouter"

    def __init__(self, verdict=PASS, error=None, delay=0.0):
        self.verdict, self.error, self.delay = verdict, error, delay
        self.calls = []

    def check(self, media, complaint, component):
        self.calls.append({"media": [(len(data), mime) for data, mime, _ in media], "complaint": complaint,
                           "component": component})
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.verdict


def photo():
    return {"kind": "image", "url": jpeg_data_url()}


class SlowSummariser(_Summariser):
    def __init__(self, text="the pack is on a table", delay=0.0):
        super().__init__(text)
        self.delay = delay

    def summarise(self, data, mime, name="video"):
        if self.delay:
            time.sleep(self.delay)
        return super().summarise(data, mime, name)


class EvidenceAtIngestTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.addCleanup(lambda: fresh_api(None))
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.checker = FakeEvidenceChecker()
        self.api.EVIDENCE_CHECKER = self.checker
        self.api.runtime.evidence_check = True
        self.seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        patch = mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: self.seen.append(m) or scripted)
        patch.start()
        self.addCleanup(patch.stop)

    def route(self, agent="battery_support", said=()):
        state = self.api.stores.conversations.get("c1")
        state.route_to(agent)
        for text in said:
            state.history.append({"role": "user", "content": text})
            state.history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        return state

    def post(self, text="here is the charger light", attachments=None, cid="c1", **extra):
        body = {"conversation_id": cid, "session_token": "sess-ananya", "text": text,
                "attachments": attachments if attachments is not None else [photo()], **extra}
        r = self.client.post("/message", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return self.seen[-1]

    def upload(self, mime="video/mp4", size=9):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": mime, "size_bytes": size,
        }).json()
        self.store.objects[body["key"]] = {"size": size, "mime": mime}
        return body

    def verdict(self, message):
        return message.entry_metadata.get("evidence_verdict")

    def test_a_photo_in_a_fault_chat_is_checked_and_the_verdict_reaches_the_turn(self):
        self.route()
        message = self.post()
        (call,) = self.checker.calls
        self.assertEqual(call["component"], "battery")
        self.assertEqual([mime for _, mime in call["media"]], ["image/jpeg"])
        self.assertIn("here is the charger light", call["complaint"])
        # With the fault it was checked for, so the runtime keeps a pass to it.
        self.assertEqual(self.verdict(message), dict(PASS.as_dict(), component="battery"))

    def test_the_complaint_is_the_customers_own_words_redacted(self):
        self.route(said=["my battery won't charge since Monday"])
        self.post(text="my number is 9999999999")
        complaint = self.checker.calls[0]["complaint"]
        self.assertTrue(complaint.startswith("my battery won't charge since Monday"))
        self.assertIn("[phone]", complaint)
        self.assertNotIn("9999999999", complaint)

    def test_a_motor_chat_is_checked_as_a_motor_fault(self):
        self.route("motor_support")
        self.post(text="here is the noise")
        self.assertEqual(self.checker.calls[0]["component"], "motor")

    def test_a_first_message_about_the_battery_is_checked(self):
        message = self.post(text="my battery won't charge, photo attached", cid="c-new")
        self.assertEqual(self.checker.calls[0]["component"], "battery")
        self.assertEqual(self.verdict(message)["passed"], True)

    def test_a_chat_that_named_the_fault_before_any_agent_is_checked(self):
        # "My motor grinds, connect me to a person" asked for a video; the
        # video that follows says nothing about the motor (the review).
        self.api.stores.conversations.get("c1").fault_topic = "motor"
        self.post(text="here it is")
        self.assertEqual(self.checker.calls[0]["component"], "motor")

    def test_nothing_is_written_outside_the_turn(self):
        self.post(text="my battery won't charge, photo attached", cid="c-new")
        self.assertIsNone(self.api.stores.conversations.peek("c-new"))

    def test_a_chat_that_is_not_about_a_fault_is_not_checked(self):
        self.route("late_warranty")
        message = self.post(text="here is my invoice, the battery one")
        self.assertEqual(self.checker.calls, [])
        self.assertIsNone(self.verdict(message))

    def test_a_first_message_with_nothing_about_a_fault_is_not_checked(self):
        message = self.post(text="hello", cid="c-new")
        self.assertEqual(self.checker.calls, [])
        self.assertIsNone(self.verdict(message))

    def test_a_turn_with_no_media_is_not_checked(self):
        self.route()
        message = self.post(text="the light is red", attachments=[])
        self.assertEqual(self.checker.calls, [])
        self.assertIsNone(self.verdict(message))

    def test_with_the_switch_off_nothing_is_checked(self):
        self.api.runtime.evidence_check = False
        self.route()
        message = self.post()
        self.assertEqual(self.checker.calls, [])
        self.assertIsNone(self.verdict(message))

    def test_switched_on_without_a_checker_is_never_a_pass(self):
        self.api.EVIDENCE_CHECKER = None
        self.route()
        self.assertEqual(self.verdict(self.post()), {"error": "not_configured"})

    def test_an_uploaded_video_and_a_photo_go_together(self):
        self.route()
        video = self.upload()
        self.post(attachments=[{"upload_id": video["upload_id"]}, photo()])
        (call,) = self.checker.calls
        self.assertEqual([mime for _, mime in call["media"]], ["video/mp4", "image/jpeg"])

    def test_an_uploaded_photo_is_read_from_the_bucket(self):
        self.route()
        image = self.upload(mime="image/png")
        self.post(attachments=[{"upload_id": image["upload_id"]}])
        self.assertEqual(self.checker.calls[0]["media"], [(len(b"\x89PNG fake"), "image/png")])
        self.assertIn(image["key"], self.store.fetched)

    def test_a_video_too_large_to_send_is_not_checked_or_fetched(self):
        self.route()
        video = self.upload(size=INLINE_LIMIT + 1)
        message = self.post(attachments=[{"upload_id": video["upload_id"]}])
        self.assertEqual(self.checker.calls, [])
        self.assertEqual(self.store.fetched, [])
        self.assertEqual(self.verdict(message), {"error": "too_large"})

    def test_a_checker_error_reaches_the_turn_as_not_checked(self):
        self.checker.error = EvidenceCheckError("unavailable")
        self.route()
        message = self.post()
        self.assertEqual(self.verdict(message), {"error": "unavailable"})
        (event,) = [e for e in self.api.log.events if e["event"] == "evidence_check_skipped"]
        self.assertEqual(event["error"], "unavailable")

    def test_an_unexpected_failure_is_a_code_too(self):
        self.checker.error = RuntimeError("provider said: my battery won't charge")
        self.route()
        self.assertEqual(self.verdict(self.post()), {"error": "RuntimeError"})
        self.assertNotIn("provider said", repr(self.api.log.events))

    def test_a_check_past_the_deadline_is_not_waited_for(self):
        self.checker.delay = 2.0
        self.route()
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.2):
            started = time.monotonic()
            message = self.post()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.verdict(message), {"error": "timeout"})

    def test_with_a_clip_it_waits_as_long_as_the_video_summary_may(self):
        self.checker.delay = 0.5
        self.route()
        video = self.upload()
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.2), \
                mock.patch.object(self.api, "VIDEO_SUMMARY_SECONDS", 3.0):
            message = self.post(attachments=[{"upload_id": video["upload_id"]}])
        self.assertEqual(self.verdict(message)["passed"], True)

    def test_with_a_clip_past_the_video_deadline_it_is_not_waited_for(self):
        self.checker.delay = 2.0
        self.route()
        video = self.upload()
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.1), \
                mock.patch.object(self.api, "VIDEO_SUMMARY_SECONDS", 0.3):
            started = time.monotonic()
            message = self.post(attachments=[{"upload_id": video["upload_id"]}])
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.verdict(message), {"error": "timeout"})

    def test_it_runs_beside_the_video_summary(self):
        self.api.VIDEO_SUMMARISER = SlowSummariser(delay=0.5)
        self.checker.delay = 0.5
        self.route()
        video = self.upload()
        started = time.monotonic()
        message = self.post(attachments=[{"upload_id": video["upload_id"]}])
        self.assertLess(time.monotonic() - started, 0.95)  # one after the other would be 1 s
        self.assertEqual(self.verdict(message)["passed"], True)
        self.assertEqual(len(self.api.VIDEO_SUMMARISER.calls), 1)

    def test_it_runs_beside_the_photo_check(self):
        self.api.PHOTO_CHECKER = FakePhotoChecker(answers=([],), delay=0.5)
        self.checker.delay = 0.5
        self.route()
        started = time.monotonic()
        message = self.post()
        self.assertLess(time.monotonic() - started, 0.95)  # one after the other would be 1 s
        self.assertEqual(self.verdict(message)["passed"], True)

    def test_a_tester_pinned_to_a_fault_agent_is_checked_on_the_first_message(self):
        message = self.post(text="here it is", cid="c-pin", agent="motor_support")
        self.assertEqual(self.checker.calls[0]["component"], "motor")
        self.assertEqual(self.verdict(message)["passed"], True)

    def test_a_pill_naming_the_fault_is_checked_on_the_first_message(self):
        self.post(text="here it is", cid="c-pill", pill="battery_issue")
        self.assertEqual(self.checker.calls[0]["component"], "battery")

    def test_a_pin_to_an_agent_that_is_not_a_fault_agent_is_not_checked(self):
        message = self.post(text="here it is", cid="c-pin", agent="late_warranty")
        self.assertEqual(self.checker.calls, [])
        self.assertIsNone(self.verdict(message))

    def test_what_gemini_wrote_is_never_logged(self):
        self.checker.verdict = FAIL
        self.route()
        message = self.post()
        self.assertEqual(self.verdict(message)["missing"], FAIL.missing)
        self.assertNotIn("SECRET", repr(self.api.log.events))
        (event,) = [e for e in self.api.log.events if e["event"] == "evidence_check_done"]
        self.assertEqual(event["passed"], False)


class SafetyDoesNotWaitTests(unittest.TestCase):
    """A safety report is immediate (the person's decision): it never waits
    for the evidence check, which safety does not use (the review of 6
    October 2026)."""

    def setUp(self):
        self.store = _Store()
        self.api = fresh_api(self.store)
        self.addCleanup(lambda: fresh_api(None))
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.checker = FakeEvidenceChecker(delay=2.0)
        self.api.EVIDENCE_CHECKER = self.checker
        self.api.runtime.evidence_check = True
        self.seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        patch = mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: self.seen.append(m) or scripted)
        patch.start()
        self.addCleanup(patch.stop)
        self.api.stores.conversations.get("c1").route_to("battery_support")

    def post(self, text, attachments):
        started = time.monotonic()
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                               "text": text, "attachments": attachments})
        self.assertEqual(r.status_code, 200, r.text)
        return self.seen[-1], time.monotonic() - started

    def upload(self):
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": "video/mp4", "size_bytes": 9,
        }).json()
        self.store.objects[body["key"]] = {"size": 9, "mime": "video/mp4"}
        return body

    def test_a_safety_text_with_a_photo_is_not_held_for_the_check(self):
        message, took = self.post("my battery is smoking", [photo()])
        self.assertLess(took, 1.0)
        self.assertEqual(self.checker.calls, [])
        self.assertNotIn("evidence_verdict", message.entry_metadata)

    def test_a_motor_hazard_in_the_text_is_not_held_either(self):
        message, took = self.post("the brakes are not working at all", [photo()])
        self.assertLess(took, 1.0)
        self.assertNotIn("evidence_verdict", message.entry_metadata)

    def test_a_photo_the_photo_check_finds_a_hazard_in_is_not_held(self):
        self.api.PHOTO_CHECKER = FakePhotoChecker(answers=(["swelling"],))
        message, took = self.post("here is the battery", [photo()])
        self.assertLess(took, 1.5)
        self.assertNotIn("evidence_verdict", message.entry_metadata)
        self.assertTrue(message.attachments[0].summary)

    def test_a_clip_whose_description_names_a_hazard_is_not_held(self):
        self.api.VIDEO_SUMMARISER = _Summariser("Smoke rises from the battery pack while it charges.")
        video = self.upload()
        message, took = self.post("here is the video", [{"upload_id": video["upload_id"]}])
        self.assertLess(took, 1.5)
        self.assertNotIn("evidence_verdict", message.entry_metadata)

    def test_a_clip_that_shows_no_hazard_is_still_checked(self):
        self.checker.delay = 0.0
        self.api.VIDEO_SUMMARISER = _Summariser("No smoke or swelling is visible; the charger light stays red.")
        video = self.upload()
        message, _ = self.post("here is the video", [{"upload_id": video["upload_id"]}])
        self.assertEqual(message.entry_metadata["evidence_verdict"]["passed"], True)


class RuntimeKeepsItTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api(_Store())
        self.addCleanup(lambda: fresh_api(None))
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.api.EVIDENCE_CHECKER = FakeEvidenceChecker(FAIL)
        self.api.runtime.evidence_check = True

    def test_the_turn_keeps_the_verdict_on_the_conversation(self):
        self.api.stores.conversations.get("c1").route_to("battery_support")
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                               "text": "here it is", "attachments": [photo()]})
        self.assertEqual(r.status_code, 200, r.text)
        verdict = self.api.stores.conversations.peek("c1").evidence_verdict
        self.assertEqual((verdict["passed"], verdict["missing"]), (False, FAIL.missing))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.api = fresh_api(None)

    def test_health_says_nothing_with_the_switch_off(self):
        self.assertNotIn("evidence_check", self.api.health())

    def test_health_says_when_it_is_on_and_whether_it_can_check(self):
        with mock.patch.object(self.api.runtime, "evidence_check", True):
            with mock.patch.object(self.api, "EVIDENCE_CHECKER", FakeEvidenceChecker()):
                self.assertEqual(self.api.health()["evidence_check"], "openrouter")
            with mock.patch.object(self.api, "EVIDENCE_CHECKER", None):
                self.assertEqual(self.api.health()["evidence_check"], "on, no checker: nothing can pass")


if __name__ == "__main__":
    unittest.main()
