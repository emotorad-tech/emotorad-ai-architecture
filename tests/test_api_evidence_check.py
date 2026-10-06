"""The evidence check at ingest (api._inbound_attachments, the person's brief
of 6 October 2026, item 2).

In a fault chat with the switch on, a turn's photos and videos go to the
checker beside the photo safety check and the video summary, under the same
deadline, and the verdict reaches the turn in entry_metadata the way
`photos_unchecked` does. Nothing is written outside the turn. The checker is
faked; no network, no AWS.
"""

import base64
import os
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from emotorad_ai.contract import Reply
from emotorad_ai.evidence_check import (
    INLINE_LIMIT,
    EvidenceCheckError,
    EvidenceVerdict,
    OpenRouterEvidenceChecker,
    read_media,
)
from tests.test_api_media_persistence import jpeg_data_url
from tests.test_evidence_shrink import FFMPEG, FakeTransport, make_clip, probe, stream
from tests.test_api_photo_check import FakeChecker as FakePhotoChecker
from tests.test_api_uploads import _Store, _Summariser, fresh_api

PASS = EvidenceVerdict(shows_part=True, fault_visible=True, matches_complaint=True,
                       seen="The charger light stays red.", missing="")
FAIL = EvidenceVerdict(shows_part=True, fault_visible=False, matches_complaint=False,
                       seen="SECRET-SEEN the pack on a table.", missing="SECRET-MISSING the charger plugged in.")


class FakeEvidenceChecker:
    provider = "openrouter"

    def __init__(self, verdict=PASS, error=None, delay=0.0, until_cancelled=0.0):
        self.verdict, self.error, self.delay = verdict, error, delay
        # Works until the turn gives up (its cancel), for at most this long.
        self.until_cancelled = until_cancelled
        self.calls = []

    def check(self, media, complaint, component, deadline_at=None, cancel=None):
        # As the real checker does: a clip still in the bucket is read.
        self.calls.append({"media": [(len(read_media(data)), mime) for data, mime, _ in media],
                           "complaint": complaint, "component": component, "deadline_at": deadline_at,
                           "cancel": cancel})
        if self.delay:
            time.sleep(self.delay)
        if self.until_cancelled:
            cancel.wait(self.until_cancelled)
        if self.error is not None:
            raise self.error
        return self.verdict


def photo():
    return {"kind": "image", "url": jpeg_data_url()}


def soon(condition, seconds=2.0):
    """Whether `condition()` holds within `seconds`: the check runs on its
    own thread, and may not have started when the turn returns."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


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

    def test_a_video_over_the_limit_goes_to_the_checker_to_be_shrunk(self):
        # Round 2 (the person's decision): a clip over the inline limit is
        # no longer refused here; the checker sends a smaller copy of it.
        self.route()
        video = self.upload(size=INLINE_LIMIT + 1)
        message = self.post(attachments=[{"upload_id": video["upload_id"]}])
        (call,) = self.checker.calls
        self.assertEqual([mime for _, mime in call["media"]], ["video/mp4"])
        self.assertEqual(self.store.fetched, [video["key"]])
        self.assertEqual(self.verdict(message)["passed"], True)

    def test_photos_too_large_to_send_are_not_checked_or_fetched(self):
        # A photo is never shrunk, so photos over the limit together are
        # refused before anything is fetched, as before.
        self.route()
        first, second = self.upload(mime="image/jpeg", size=INLINE_LIMIT // 2 + 1), \
            self.upload(mime="image/jpeg", size=INLINE_LIMIT // 2 + 1)
        message = self.post(attachments=[{"upload_id": first["upload_id"]}, {"upload_id": second["upload_id"]}])
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

    def test_the_check_is_given_the_turns_deadline(self):
        # The turn's wait starts before the clip is read from the bucket, so
        # the checker is told when it ends (the re-review of 6 October 2026).
        self.route()
        video = self.upload()
        with mock.patch.object(self.api, "VIDEO_SUMMARY_SECONDS", 3.0):
            started = time.monotonic()
            self.post(attachments=[{"upload_id": video["upload_id"]}])
            finished = time.monotonic()
        deadline_at = self.checker.calls[0]["deadline_at"]
        self.assertGreaterEqual(deadline_at, started + 3.0)
        self.assertLessEqual(deadline_at, finished + 3.0)

    def test_a_photos_check_is_given_the_photo_checks_deadline(self):
        self.route()
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 2.0):
            started = time.monotonic()
            self.post()
            finished = time.monotonic()
        deadline_at = self.checker.calls[0]["deadline_at"]
        self.assertGreaterEqual(deadline_at, started + 2.0)
        self.assertLessEqual(deadline_at, finished + 2.0)

    def test_a_turn_that_stops_waiting_tells_the_check_to_stop(self):
        self.checker.until_cancelled = 3.0
        self.route()
        with mock.patch.object(self.api, "PHOTO_CHECK_DEADLINE_SECONDS", 0.2):
            started = time.monotonic()
            message = self.post()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.verdict(message), {"error": "timeout"})
        self.assertTrue(self.checker.calls[0]["cancel"].is_set())

    def test_a_check_that_answers_in_time_is_not_told_to_stop(self):
        self.route()
        self.post()
        self.assertFalse(self.checker.calls[0]["cancel"].is_set())

    def test_a_turn_that_fails_after_the_check_started_tells_it_to_stop(self):
        class Broken(_Summariser):
            def summarise(self, data, mime, name="video"):
                raise RuntimeError("the summariser broke")

        self.api.VIDEO_SUMMARISER = Broken()
        self.checker.until_cancelled = 3.0
        self.route()
        video = self.upload()
        client = TestClient(self.api.app, raise_server_exceptions=False)
        r = client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "here",
                                          "attachments": [{"upload_id": video["upload_id"]}]})
        self.assertEqual(r.status_code, 500)
        self.assertTrue(soon(lambda: self.checker.calls))
        self.assertTrue(soon(lambda: self.checker.calls[0]["cancel"].is_set()))

    def test_an_uploaded_clip_reaches_the_checker_still_in_the_bucket(self):
        # Streamed by the checker when it is shrunk, never all read at once
        # here (the re-review of 6 October 2026).
        self.route()
        video = self.upload(size=INLINE_LIMIT + 1)
        loads = []
        real = self.checker.check

        def check(media, complaint, component, **kwargs):
            loads.extend(type(data).__name__ for data, _, _ in media)
            return real(media, complaint, component, **kwargs)

        self.checker.check = check
        self.post(attachments=[{"upload_id": video["upload_id"]}, photo()])
        self.assertEqual(loads, ["StoredClip", "bytes"])

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

    def test_a_check_dropped_for_a_safety_report_is_told_to_stop(self):
        self.checker.delay, self.checker.until_cancelled = 0.0, 3.0
        self.api.PHOTO_CHECKER = FakePhotoChecker(answers=(["swelling"],))
        self.post("here is the battery", [photo()])
        self.assertTrue(soon(lambda: self.checker.calls))
        self.assertTrue(soon(lambda: self.checker.calls[0]["cancel"].is_set()))

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


class _ClipStore(_Store):
    """The fake bucket, holding real bytes for the clips put in it."""

    def __init__(self):
        super().__init__()
        self.data, self.puts, self.streamed = {}, [], []

    def get_bytes(self, key):
        self.fetched.append(key)
        return self.data.get(key, b"\x89PNG fake")

    def copy_to(self, key, handle):
        """S3Store.copy_to: the object written into a file in parts."""
        self.streamed.append(key)
        data = self.data.get(key, b"\x89PNG fake")
        for start in range(0, len(data), 65536):
            handle.write(data[start:start + 65536])

    def put_bytes(self, key, data, mime):
        self.puts.append((key, len(data), mime))
        super().put_bytes(key, data, mime)


@unittest.skipUnless(FFMPEG, "the bundled ffmpeg (imageio-ffmpeg) is not installed")
class ShrinkAtIngestTests(unittest.TestCase):
    """The real checker with a fake transport and a lowered limit: a big clip
    is checked as a smaller copy, and the customer's original is what the
    bucket keeps, what the turn attaches (so what Zoho is sent) and what the
    media record describes. The copy reaches no log and no store."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.clip = make_clip(cls.folder.name, "big.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.store = _ClipStore()
        self.api = fresh_api(self.store)
        self.addCleanup(lambda: fresh_api(None))
        self.client = TestClient(self.api.app)
        self.api.VIDEO_SUMMARISER = None
        self.api.PHOTO_CHECKER = None
        self.transport = FakeTransport()
        self.api.runtime.evidence_check = True
        self.seen = []
        scripted = Reply(conversation_id="c1", text="ok", handled_by="test")
        patch = mock.patch.object(self.api.runtime, "handle", side_effect=lambda m: self.seen.append(m) or scripted)
        patch.start()
        self.addCleanup(patch.stop)
        self.api.stores.conversations.get("c1").route_to("motor_support")

    def send_clip(self, **checker):
        self.api.EVIDENCE_CHECKER = OpenRouterEvidenceChecker(
            transport=self.transport, inline_limit=len(self.clip) // 4, **checker)
        body = self.client.post("/uploads", json={
            "session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers",
            "mime_type": "video/mp4", "size_bytes": len(self.clip),
        }).json()
        self.store.objects[body["key"]] = {"size": len(self.clip), "mime": "video/mp4"}
        self.store.data[body["key"]] = self.clip
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya",
                                               "text": "listen to the grinding",
                                               "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 200, r.text)
        return self.seen[-1], body["key"]

    def test_the_check_gets_the_copy_and_everything_else_keeps_the_original(self):
        message, key = self.send_clip()
        self.assertEqual(message.entry_metadata["evidence_verdict"]["passed"], True)
        [(sent, mime)] = self.transport.sent()
        self.assertLess(len(sent), len(self.clip) // 4)
        self.assertIn("aac", stream(probe(sent), "Audio"))
        # The bucket: the original only, untouched, and nothing written.
        self.assertEqual(self.store.data, {key: self.clip})
        self.assertEqual(self.store.objects[key]["size"], len(self.clip))
        self.assertEqual(self.store.puts, [])
        # The turn attaches the original's key, which the Zoho worker reads.
        self.assertEqual([a.url for a in message.attachments], ["s3://" + key])
        # The permanent media record describes the original.
        [record] = self.api.stores.conversations.media_of("c1")
        self.assertEqual((record["key"], record["size_bytes"]), (key, len(self.clip)))
        # No log line carries the copy or its size.
        logged = repr(self.api.log.events)
        self.assertNotIn(base64.b64encode(sent[:60]).decode()[:40], logged)
        self.assertNotIn(str(len(sent)), logged)

    def test_the_clip_is_streamed_from_the_bucket_into_the_shrink(self):
        message, key = self.send_clip()
        self.assertEqual(message.entry_metadata["evidence_verdict"]["passed"], True)
        self.assertEqual((self.store.streamed, self.store.fetched), ([key], []))

    def test_a_safety_report_stops_the_shrink_and_nothing_is_sent(self):
        # The turn drops its wait for a safety report; the copy that would
        # have been made 0.6 s later never reaches OpenRouter.
        self.api.VIDEO_SUMMARISER = _Summariser("Smoke rises from the battery pack while it charges.")
        slow = self.fake_ffmpeg("slow", 'sleep 0.6; for a; do out="$a"; done; head -c 100 /dev/zero > "$out"')
        started = time.monotonic()
        message, _ = self.send_clip(ffmpeg=slow)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertNotIn("evidence_verdict", message.entry_metadata)
        time.sleep(1.2)
        self.assertEqual(self.transport.posts, [])

    def test_a_turn_that_stopped_waiting_sends_nothing_later(self):
        # The turn waits 0.5 s; the copy would be ready at 0.8 s. Nothing is
        # sent after the turn has stopped waiting.
        slow = self.fake_ffmpeg("slower", 'sleep 0.8; for a; do out="$a"; done; head -c 100 /dev/zero > "$out"')
        with mock.patch.object(self.api, "VIDEO_SUMMARY_SECONDS", 0.5):
            message, _ = self.send_clip(ffmpeg=slow)
        self.assertIn("error", message.entry_metadata["evidence_verdict"])
        time.sleep(1.2)
        self.assertEqual(self.transport.posts, [])

    def fake_ffmpeg(self, name, body):
        path = os.path.join(self.folder.name, "ffmpeg-" + name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def test_a_shrink_that_fails_reaches_the_turn_as_not_checked(self):
        failing = os.path.join(self.folder.name, "ffmpeg-fails")
        with open(failing, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(failing, 0o755)
        message, _ = self.send_clip(ffmpeg=failing)
        self.assertEqual(message.entry_metadata["evidence_verdict"], {"error": "transcode_failed"})
        self.assertEqual(self.transport.posts, [])

    def test_a_shrink_that_runs_too_long_reaches_the_turn_as_not_checked(self):
        hang = os.path.join(self.folder.name, "ffmpeg-hangs")
        with open(hang, "w") as handle:
            handle.write("#!/bin/sh\nexec sleep 10\n")
        os.chmod(hang, 0o755)
        started = time.monotonic()
        message, _ = self.send_clip(ffmpeg=hang, shrink_seconds=0.3)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(message.entry_metadata["evidence_verdict"], {"error": "transcode_timeout"})
        self.assertEqual(self.transport.posts, [])


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
