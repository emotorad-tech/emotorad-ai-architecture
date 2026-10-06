"""The evidence check stays inside the turn's wait, and inside the server
(the re-review of 6 October 2026).

1. The turn's deadline is the check's deadline. The API starts its wait
   before the clips are read from the bucket, so the checker is handed that
   moment (`deadline_at`) and takes the shrink budget and the request's
   timeout from it, not from when it was entered. Too little left, and the
   request is not sent at all. A turn that stops waiting (its deadline, or a
   safety report) sets `cancel`: ffmpeg is stopped and nothing is sent.
2. Big clips cannot take the server down. At most TRANSCODE_SLOTS transcodes
   run at a time (a turn that cannot get a slot within its budget is
   `transcode_timeout`), each ffmpeg on two threads, and a clip still in the
   bucket (`StoredClip`) is streamed straight into ffmpeg's temporary file,
   never held in memory whole.

The transport is faked and ffmpeg is a shell script or the bundled one, as
in tests/test_evidence_shrink.py. No network.
"""

import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from emotorad_ai import evidence_check
from emotorad_ai.evidence_check import (
    MIN_REQUEST_SECONDS,
    TRANSCODE_SLOTS,
    TRANSCODE_THREADS,
    EvidenceCheckError,
    OpenRouterEvidenceChecker,
    StoredClip,
    read_media,
    shrink_args,
)
from tests.test_evidence_shrink import COMPLAINT, FFMPEG, PHOTO, FakeTransport, fake_ffmpeg, make_clip, probe, stream


def checker(transport, **kwargs):
    return OpenRouterEvidenceChecker(transport=transport, **kwargs)


class Folder(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        # Over the lowered limit below, so it is always shrunk.
        self.clip = (b"\x00\x00\x00 ftypmp4" + b"x" * 5000, "video/mp4", "clip.mp4")

    def ffmpeg(self, body):
        return fake_ffmpeg(self.folder.name, body)

    def writes_a_copy(self, before=""):
        """A stand-in ffmpeg that writes a 100-byte copy to its last argument."""
        return self.ffmpeg(before + 'for a; do out="$a"; done; head -c 100 /dev/zero > "$out"')


class TheTurnsDeadlineTests(Folder):
    def test_the_shrink_stops_at_the_turns_deadline_not_its_own(self):
        # 45 s of its own, but the turn waits only 1 s more: ffmpeg gets half
        # of that and is stopped, inside the turn's wait.
        hang = self.ffmpeg("exec sleep 10")
        transport = FakeTransport()
        started = time.monotonic()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=1000, ffmpeg=hang).check(
                [self.clip], COMPLAINT, "motor", deadline_at=started + 1.0)
        self.assertEqual(str(raised.exception), "transcode_timeout")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(transport.posts, [])

    def test_the_request_waits_only_until_the_turns_deadline(self):
        transport = FakeTransport()
        deadline_at = time.monotonic() + 5.0
        checker(transport).check([PHOTO], COMPLAINT, "battery", deadline_at=deadline_at)
        timeout = transport.posts[0]["timeout"]
        self.assertLessEqual(timeout, 5.0)
        self.assertGreater(timeout, 4.0)

    def test_after_a_shrink_the_request_has_what_is_left_of_the_turns_deadline(self):
        transport = FakeTransport()
        deadline_at = time.monotonic() + 6.0
        checker(transport, inline_limit=1000, ffmpeg=self.writes_a_copy("sleep 0.5; ")).check(
            [self.clip], COMPLAINT, "motor", deadline_at=deadline_at)
        timeout = transport.posts[0]["timeout"]
        self.assertLessEqual(timeout, 5.5)
        self.assertGreater(timeout, 4.0)

    def test_its_own_deadline_still_holds_when_the_turn_waits_longer(self):
        transport = FakeTransport()
        checker(transport, deadline_seconds=12.0).check([PHOTO], COMPLAINT, "battery",
                                                        deadline_at=time.monotonic() + 60.0)
        self.assertLessEqual(transport.posts[0]["timeout"], 12.0)

    def test_a_deadline_already_passed_sends_nothing(self):
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport).check([PHOTO], COMPLAINT, "battery", deadline_at=time.monotonic() - 1.0)
        self.assertEqual((str(raised.exception), transport.posts), ("timeout", []))
        self.assertIsNone(raised.exception.__cause__)

    def test_too_little_left_for_an_answer_sends_nothing(self):
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport).check([PHOTO], COMPLAINT, "battery",
                                     deadline_at=time.monotonic() + MIN_REQUEST_SECONDS / 2)
        self.assertEqual((str(raised.exception), transport.posts), ("timeout", []))

    def test_a_shrink_that_uses_up_the_turn_sends_nothing(self):
        # The copy is made, but too late for an answer to be read.
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError):
            checker(transport, inline_limit=1000, ffmpeg=self.writes_a_copy("sleep 0.4; ")).check(
                [self.clip], COMPLAINT, "motor", deadline_at=time.monotonic() + 1.2)
        self.assertEqual(transport.posts, [])

    def test_without_a_turn_the_whole_deadline_is_the_requests(self):
        transport = FakeTransport()
        checker(transport, deadline_seconds=12.0).check([PHOTO], COMPLAINT, "battery")
        self.assertEqual(transport.posts[0]["timeout"], 12.0)


class CancelTests(Folder):
    def test_a_cancel_stops_ffmpeg_and_nothing_is_sent(self):
        hang = self.ffmpeg("exec sleep 10")
        transport = FakeTransport()
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=1000, ffmpeg=hang, shrink_seconds=10.0).check(
                [self.clip], COMPLAINT, "motor", cancel=cancel)
        self.assertEqual(str(raised.exception), "cancelled")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(transport.posts, [])
        self.assertIsNone(raised.exception.__cause__)

    def test_a_check_cancelled_before_its_request_sends_nothing(self):
        cancel = threading.Event()
        cancel.set()
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport).check([PHOTO], COMPLAINT, "battery", cancel=cancel)
        self.assertEqual((str(raised.exception), transport.posts), ("cancelled", []))

    def test_a_cancel_after_the_copy_is_made_sends_nothing(self):
        # The copy is made; the turn gives up just after, before the request.
        cancel = threading.Event()
        fit_inline = evidence_check.fit_inline

        def then_give_up(*args, **kwargs):
            fitted = fit_inline(*args, **kwargs)
            cancel.set()
            return fitted

        transport = FakeTransport()
        with mock.patch.object(evidence_check, "fit_inline", side_effect=then_give_up):
            with self.assertRaises(EvidenceCheckError) as raised:
                checker(transport, inline_limit=1000, ffmpeg=self.writes_a_copy()).check(
                    [self.clip], COMPLAINT, "motor", cancel=cancel)
        self.assertEqual((str(raised.exception), transport.posts), ("cancelled", []))

    def test_an_unused_cancel_changes_nothing(self):
        transport = FakeTransport()
        verdict = checker(transport).check([PHOTO], COMPLAINT, "battery", cancel=threading.Event())
        self.assertTrue(verdict.passed)
        self.assertEqual(len(transport.posts), 1)


class SlotTests(Folder):
    def test_a_turn_that_cannot_get_a_slot_in_its_budget_is_a_transcode_timeout(self):
        ran = os.path.join(self.folder.name, "ran")
        never = self.ffmpeg("touch '%s'; exit 1" % ran)
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        self.addCleanup(slots.release)
        transport = FakeTransport()
        started = time.monotonic()
        with mock.patch.object(evidence_check, "_TRANSCODE_SLOTS", slots):
            with self.assertRaises(EvidenceCheckError) as raised:
                checker(transport, inline_limit=1000, ffmpeg=never, shrink_seconds=1.0).check(
                    [self.clip], COMPLAINT, "motor")
        self.assertEqual(str(raised.exception), "transcode_timeout")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(os.path.exists(ran))
        self.assertEqual(transport.posts, [])

    def test_a_turn_waiting_for_a_slot_is_cancelled_too(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        self.addCleanup(slots.release)
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with mock.patch.object(evidence_check, "_TRANSCODE_SLOTS", slots):
            with self.assertRaises(EvidenceCheckError) as raised:
                checker(FakeTransport(), inline_limit=1000, ffmpeg=self.writes_a_copy(),
                        shrink_seconds=10.0).check([self.clip], COMPLAINT, "motor", cancel=cancel)
        self.assertEqual(str(raised.exception), "cancelled")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_no_more_than_the_slots_transcode_at_once(self):
        log = os.path.join(self.folder.name, "log")
        logs = self.writes_a_copy("echo s >> '%s'; sleep 0.3; echo e >> '%s'; " % (log, log))
        results = []

        def one():
            transport = FakeTransport()
            try:
                checker(transport, inline_limit=1000, ffmpeg=logs, shrink_seconds=10.0).check(
                    [self.clip], COMPLAINT, "motor")
                results.append("passed")
            except EvidenceCheckError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=one) for _ in range(TRANSCODE_SLOTS + 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(results, ["passed"] * (TRANSCODE_SLOTS + 2))
        running = most = 0
        with open(log) as handle:
            for line in handle:
                running += 1 if line.strip() == "s" else -1
                most = max(most, running)
        self.assertEqual(most, TRANSCODE_SLOTS)

    def test_the_slot_is_given_back_whatever_happens(self):
        fails = self.ffmpeg("exit 1")
        hang = self.ffmpeg("exec sleep 10")
        for kwargs in ({"ffmpeg": fails}, {"ffmpeg": hang, "shrink_seconds": 0.2}, {"ffmpeg": self.writes_a_copy()}):
            try:
                checker(FakeTransport(), inline_limit=1000, **kwargs).check([self.clip], COMPLAINT, "motor")
            except EvidenceCheckError:
                pass
        taken = [evidence_check._TRANSCODE_SLOTS.acquire(blocking=False) for _ in range(TRANSCODE_SLOTS)]
        for took in taken:
            if took:
                evidence_check._TRANSCODE_SLOTS.release()
        self.assertEqual(taken, [True] * TRANSCODE_SLOTS)

    def test_one_or_two_slots_and_two_threads(self):
        self.assertIn(TRANSCODE_SLOTS, (1, 2))
        self.assertEqual(TRANSCODE_THREADS, 2)

    def test_ffmpeg_decodes_filters_and_encodes_on_two_threads(self):
        args = shrink_args("ffmpeg", "in.mov", "out.mp4", evidence_check.SHRINK_STEPS[0])
        source = args.index("-i")
        before, after = args[:source], args[source + 2:]
        self.assertIn(["-threads", "2"], [before[n:n + 2] for n in range(len(before))])
        self.assertIn(["-threads", "2"], [after[n:n + 2] for n in range(len(after))])
        self.assertIn(["-filter_threads", "2"], [before[n:n + 2] for n in range(len(before))])


class StoredClipTests(Folder):
    def stored(self, data, handles=None):
        def copy_to(handle):
            if handles is not None:
                handles.append(handle)
            # In parts, as a bucket sends it.
            for start in range(0, len(data), 1024):
                handle.write(data[start:start + 1024])
        return StoredClip(size=len(data), copy_to=copy_to)

    def test_a_clip_still_in_the_bucket_goes_straight_into_ffmpegs_file(self):
        handles = []
        transport = FakeTransport()
        checker(transport, inline_limit=1000, ffmpeg=self.writes_a_copy()).check(
            [(self.stored(self.clip[0], handles), "video/mp4", "clip.mp4")], COMPLAINT, "motor")
        # Written into the temporary file ffmpeg reads, never into memory.
        [handle] = handles
        self.assertNotIsInstance(handle, io.BytesIO)
        self.assertTrue(handle.name.endswith("clip.mp4"))
        self.assertEqual(transport.sent(), [(b"\x00" * 100, "video/mp4")])

    def test_a_clip_still_in_the_bucket_under_the_limit_is_read_and_sent_as_it_is(self):
        transport = FakeTransport()
        checker(transport).check([(self.stored(self.clip[0]), "video/quicktime", "clip.mov"), PHOTO],
                                 COMPLAINT, "motor")
        self.assertEqual(transport.sent(), [(self.clip[0], "video/quicktime"), (PHOTO[0], PHOTO[1])])

    def test_its_size_decides_without_reading_it(self):
        reads = []
        clip = StoredClip(size=10_000, copy_to=lambda handle: reads.append(handle))
        photo = (b"p" * 3000, "image/jpeg", "a.jpg")
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(FakeTransport(), inline_limit=5000).check([photo, photo, (clip, "video/mp4", "c.mp4")],
                                                              COMPLAINT, "motor")
        self.assertEqual((str(raised.exception), reads), ("too_large", []))

    def test_read_media_reads_either_kind(self):
        self.assertEqual(read_media(b"abc"), b"abc")
        self.assertEqual(read_media(self.stored(b"abc" * 1000)), b"abc" * 1000)

    @unittest.skipUnless(FFMPEG, "the bundled ffmpeg (imageio-ffmpeg) is not installed")
    def test_a_real_clip_streamed_from_the_bucket_is_shrunk_with_its_sound(self):
        big = make_clip(self.folder.name, "big.mp4")
        transport = FakeTransport()
        checker(transport, inline_limit=len(big) // 4).check([(self.stored(big), "video/mp4", "big.mp4")],
                                                             COMPLAINT, "motor")
        [(sent, mime)] = transport.sent()
        self.assertEqual(mime, "video/mp4")
        self.assertLess(len(sent), len(big) // 4)
        self.assertIn("aac", stream(probe(sent), "Audio"))


if __name__ == "__main__":
    unittest.main()
