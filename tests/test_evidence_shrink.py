"""A clip too big to send inline is checked as a smaller copy (the person's
decision, 6 October 2026).

OpenRouter takes media only inline, up to 14 MiB, and most phone videos are
bigger. So the checker makes a copy with the ffmpeg the repo already bundles:
the shorter side at most 480 px (360 px on a second try), ordinary 8-bit
H.264, the sound kept as AAC mono, because a noise is often the fault. The
customer's original is never changed. A copy that cannot be made, or takes too
long, is a check error: never a pass.

Clips are generated with the bundled ffmpeg, as tests/test_video.py does, and
are a second or two long. The transport is faked: no network.
"""

import base64
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from emotorad_ai import evidence_check
from emotorad_ai.evidence_check import (
    DEADLINE_SECONDS,
    INLINE_LIMIT,
    SHRINK_SECONDS,
    SHRINK_STEPS,
    EvidenceCheckError,
    OpenRouterEvidenceChecker,
    shrink_video,
    verdict_record,
)

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover - only on a machine without the bundled ffmpeg
    FFMPEG = None

PASS = {"shows_part": True, "fault_visible": True, "matches_complaint": True,
        "seen": "The motor grinds when the pedals turn.", "missing": ""}
COMPLAINT = "My motor makes a grinding noise when I pedal."
PHOTO = (b"\xff\xd8jpeg photo", "image/jpeg", "photo.jpg")


class FakeTransport:
    def __init__(self):
        self.posts = []

    def post(self, path, body, timeout=None):
        self.posts.append({"path": path, "body": body, "timeout": timeout})
        return {"model": "m", "choices": [{"finish_reason": "stop",
                                           "message": {"role": "assistant", "content": json.dumps(PASS)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def sent(self):
        """(bytes, mime) of every photo and video in the one request."""
        content = self.posts[0]["body"]["messages"][0]["content"]
        out = []
        for part in content:
            url = (part.get("video_url") or part.get("image_url") or {}).get("url")
            if url:
                head, data = url.split(",", 1)
                out.append((base64.b64decode(data), head[len("data:"):-len(";base64")]))
        return out


def run_ffmpeg(*args):
    result = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", "replace")[-300:])


def make_clip(folder, name, size="1280x720", seconds=1, video=("-c:v", "libx264", "-crf", "0", "-preset",
              "ultrafast", "-pix_fmt", "yuv420p"), extra=()):
    """A clip with a moving picture and a tone; lossless, so it is big."""
    path = os.path.join(folder, name)
    run_ffmpeg("-f", "lavfi", "-i", "testsrc2=s=%s:d=%s:r=30" % (size, seconds),
               "-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % seconds,
               *video, "-c:a", "aac", "-ac", "2", *extra, "-shortest", path)
    with open(path, "rb") as handle:
        return handle.read()


def probe(data):
    """What ffmpeg says about a clip: its streams and any metadata lines."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "probe.mp4")
        with open(path, "wb") as handle:
            handle.write(data)
        result = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True)
    return result.stderr.decode("utf-8", "replace")


def stream(info, kind):
    lines = [line.strip() for line in info.splitlines() if "Stream #" in line and kind + ":" in line]
    return lines[0] if lines else ""


def size_of(line):
    """(width, height) from a probe's video stream line."""
    for token in line.replace(",", " ").split():
        width, x, height = token.partition("x")
        if x and width.isdigit() and height.isdigit():
            return int(width), int(height)
    return None


def fake_ffmpeg(folder, body):
    """A stand-in ffmpeg: a shell script, so a failure or a hang is certain."""
    path = os.path.join(folder, "ffmpeg")
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def checker(transport, **kwargs):
    return OpenRouterEvidenceChecker(transport=transport, **kwargs)


@unittest.skipUnless(FFMPEG, "the bundled ffmpeg (imageio-ffmpeg) is not installed")
class ShrinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.big = make_clip(cls.folder.name, "big.mp4")
        cls.small = make_clip(cls.folder.name, "small.mp4", size="320x180", video=(
            "-c:v", "libx264", "-crf", "35", "-pix_fmt", "yuv420p"))

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def video(self, data=None, mime="video/mp4"):
        return (self.big if data is None else data, mime, "clip.mp4")

    def test_a_clip_over_the_limit_is_sent_as_a_smaller_copy(self):
        transport = FakeTransport()
        limit = len(self.big) // 4
        verdict = checker(transport, inline_limit=limit).check([self.video()], COMPLAINT, "motor")
        self.assertTrue(verdict.passed)
        [(sent, mime)] = transport.sent()
        self.assertEqual(mime, "video/mp4")
        self.assertLess(len(sent), limit)
        self.assertNotEqual(sent, self.big)
        picture = stream(probe(sent), "Video")
        self.assertIn("h264", picture)
        self.assertIn("yuv420p", picture)
        self.assertEqual(min(size_of(picture)), 480)

    def test_the_copy_keeps_the_sound_as_aac_mono(self):
        transport = FakeTransport()
        checker(transport, inline_limit=len(self.big) // 4).check([self.video()], COMPLAINT, "motor")
        [(sent, _)] = transport.sent()
        sound = stream(probe(sent), "Audio")
        self.assertIn("aac", sound)
        self.assertIn("mono", sound)

    def test_the_customers_original_is_not_changed(self):
        original = bytes(self.big)
        media = [self.video()]
        checker(FakeTransport(), inline_limit=len(self.big) // 4).check(media, COMPLAINT, "motor")
        self.assertEqual(media[0][0], original)

    def test_a_clip_under_the_limit_is_sent_as_it_is(self):
        transport = FakeTransport()
        checker(transport).check([self.video(self.small, "video/quicktime")], COMPLAINT, "motor")
        self.assertEqual(transport.sent(), [(self.small, "video/quicktime")])

    def test_a_clip_still_too_big_at_480_is_tried_at_360(self):
        at_480 = shrink_video(self.big, SHRINK_STEPS[0], timeout=30)
        transport = FakeTransport()
        checker(transport, inline_limit=len(at_480) - 1).check([self.video()], COMPLAINT, "motor")
        [(sent, _)] = transport.sent()
        self.assertLess(len(sent), len(at_480))
        self.assertEqual(min(size_of(stream(probe(sent), "Video"))), 360)
        self.assertIn("aac", stream(probe(sent), "Audio"))

    def test_a_clip_still_too_big_at_360_is_too_large_and_nothing_is_sent(self):
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=1000).check([self.video()], COMPLAINT, "motor")
        self.assertEqual((str(raised.exception), transport.posts), ("too_large", []))

    def test_a_portrait_clip_is_turned_upright_before_its_shorter_side_is_cut(self):
        # A phone held upright records a landscape picture with a rotation.
        rotated = os.path.join(self.folder.name, "rotated.mp4")
        run_ffmpeg("-display_rotation", "90", "-i", os.path.join(self.folder.name, "big.mp4"), "-c", "copy", rotated)
        with open(rotated, "rb") as handle:
            data = handle.read()
        self.assertIn("rotation", probe(data))
        transport = FakeTransport()
        checker(transport, inline_limit=len(data) // 4).check([self.video(data)], COMPLAINT, "motor")
        [(sent, _)] = transport.sent()
        width, height = size_of(stream(probe(sent), "Video"))
        self.assertEqual(width, 480)
        self.assertGreater(height, width)

    def test_a_ten_bit_hevc_clip_becomes_ordinary_eight_bit_h264(self):
        encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True).stdout
        if b"libx265" not in encoders:
            self.skipTest("this ffmpeg cannot make a 10-bit HEVC clip to test with")
        hdr = make_clip(self.folder.name, "hdr.mov", size="854x480", video=(
            "-c:v", "libx265", "-x265-params", "log-level=error", "-tag:v", "hvc1", "-pix_fmt", "yuv420p10le"))
        self.assertIn("yuv420p10le", stream(probe(hdr), "Video"))
        transport = FakeTransport()
        checker(transport, inline_limit=len(hdr) - 1).check([self.video(hdr, "video/quicktime")], COMPLAINT,
                                                             "motor")
        [(sent, mime)] = transport.sent()
        picture = stream(probe(sent), "Video")
        self.assertEqual(mime, "video/mp4")
        self.assertIn("h264", picture)
        self.assertIn("yuv420p(", picture)
        self.assertNotIn("10le", picture)

    def test_the_copy_does_not_carry_where_the_clip_was_taken(self):
        tagged = make_clip(self.folder.name, "tagged.mp4", extra=("-metadata", "location=+12.9716+077.5946/"))
        self.assertIn("12.9716", probe(tagged))
        transport = FakeTransport()
        checker(transport, inline_limit=len(tagged) // 4).check([self.video(tagged)], COMPLAINT, "motor")
        [(sent, _)] = transport.sent()
        self.assertNotIn("12.9716", probe(sent))

    def test_photos_are_sent_as_they_are_beside_a_shrunk_clip(self):
        transport = FakeTransport()
        checker(transport, inline_limit=len(self.big) // 4).check([self.video(), PHOTO], COMPLAINT, "motor")
        sent = transport.sent()
        self.assertEqual(sent[1], (PHOTO[0], PHOTO[1]))
        self.assertLess(len(sent[0][0]), len(self.big))

    def test_the_request_waits_only_for_what_is_left_of_the_deadline(self):
        transport = FakeTransport()
        started = time.monotonic()
        checker(transport, inline_limit=len(self.big) // 4, deadline_seconds=40.0).check(
            [self.video()], COMPLAINT, "motor")
        took = time.monotonic() - started
        timeout = transport.posts[0]["timeout"]
        self.assertLess(timeout, 40.0)
        self.assertGreaterEqual(timeout, 40.0 - took - 0.01)

    def test_a_clip_ffmpeg_cannot_read_is_a_check_error_never_a_pass(self):
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=1000).check([self.video(b"x" * 5000)], COMPLAINT, "motor")
        self.assertEqual((str(raised.exception), transport.posts), ("transcode_failed", []))
        self.assertIsNone(raised.exception.__cause__)
        self.assertFalse(verdict_record({"error": str(raised.exception)}, at="T")["passed"])

    def test_the_temp_files_are_deleted_whatever_happens(self):
        with tempfile.TemporaryDirectory() as scratch:
            with mock.patch.object(tempfile, "tempdir", scratch):
                checker(FakeTransport(), inline_limit=len(self.big) // 4).check([self.video()], COMPLAINT, "motor")
                with self.assertRaises(EvidenceCheckError):
                    checker(FakeTransport(), inline_limit=1000).check([self.video(b"x" * 5000)], COMPLAINT,
                                                                      "motor")
                hang = fake_ffmpeg(self.folder.name, "exec sleep 10")
                with self.assertRaises(EvidenceCheckError):
                    checker(FakeTransport(), inline_limit=1000, ffmpeg=hang, shrink_seconds=0.3).check(
                        [self.video()], COMPLAINT, "motor")
                self.assertEqual(os.listdir(scratch), [])


class ShrinkFailureTests(unittest.TestCase):
    """A copy that cannot be made is a check error, never a pass, and nothing
    is sent. These need no real ffmpeg."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clip = (b"\x00\x00\x00 ftypmp4" + b"x" * 5000, "video/mp4", "clip.mp4")

    def check(self, **kwargs):
        transport = FakeTransport()
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=1000, **kwargs).check([self.clip], COMPLAINT, "motor")
        self.assertEqual(transport.posts, [])
        self.assertIsNone(raised.exception.__cause__)
        return str(raised.exception)

    def test_ffmpeg_failing_is_a_check_error(self):
        self.assertEqual(self.check(ffmpeg=fake_ffmpeg(self.folder.name, "echo 'secret detail' >&2; exit 1")),
                         "transcode_failed")

    def test_ffmpeg_writing_nothing_is_a_check_error(self):
        self.assertEqual(self.check(ffmpeg=fake_ffmpeg(self.folder.name, "exit 0")), "transcode_failed")

    def test_no_ffmpeg_at_all_is_a_check_error(self):
        self.assertEqual(self.check(ffmpeg=os.path.join(self.folder.name, "missing")), "transcode_failed")
        with mock.patch("emotorad_ai.video.ffmpeg_exe", return_value=None):
            self.assertEqual(self.check(), "transcode_failed")

    def test_a_transcode_that_runs_too_long_is_stopped_and_is_a_check_error(self):
        hang = fake_ffmpeg(self.folder.name, "exec sleep 10")
        started = time.monotonic()
        self.assertEqual(self.check(ffmpeg=hang, shrink_seconds=0.3), "transcode_timeout")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_copy_no_smaller_than_its_clip_is_not_used(self):
        # Two clips over the limit together: the big one's copy helps, the
        # small one's copy (3,000 bytes from a 1,000-byte clip) would not.
        writes = fake_ffmpeg(self.folder.name, 'for a; do out="$a"; done; head -c 3000 /dev/zero > "$out"')
        big = (b"b" * 20000, "video/mp4", "big.mp4")
        small = (b"s" * 1000, "video/webm", "small.webm")
        transport = FakeTransport()
        checker(transport, inline_limit=5000, ffmpeg=writes).check([big, small], COMPLAINT, "motor")
        self.assertEqual(transport.sent(), [(b"\x00" * 3000, "video/mp4"), (small[0], "video/webm")])

    def test_photos_alone_over_the_limit_are_too_large_with_no_transcode(self):
        never = fake_ffmpeg(self.folder.name, "touch '%s'; exit 1" % os.path.join(self.folder.name, "ran"))
        transport = FakeTransport()
        photo = (b"p" * 3000, "image/jpeg", "a.jpg")
        with self.assertRaises(EvidenceCheckError) as raised:
            checker(transport, inline_limit=5000, ffmpeg=never).check([photo, photo, self.clip], COMPLAINT, "motor")
        self.assertEqual((str(raised.exception), transport.posts), ("too_large", []))
        self.assertFalse(os.path.exists(os.path.join(self.folder.name, "ran")))

    def test_the_shrink_fits_inside_the_checks_deadline(self):
        self.assertLess(SHRINK_SECONDS, DEADLINE_SECONDS)
        self.assertEqual(checker(FakeTransport()).shrink_budget, min(SHRINK_SECONDS, DEADLINE_SECONDS / 2))
        self.assertEqual(checker(FakeTransport(), deadline_seconds=12.0).shrink_budget, 6.0)
        self.assertEqual(checker(FakeTransport()).inline_limit, INLINE_LIMIT)

    def test_the_steps_are_the_persons(self):
        self.assertEqual([step[0] for step in SHRINK_STEPS], [480, 360])
        self.assertEqual(SHRINK_STEPS[0][2:], ("700k", "1400k"))
        self.assertEqual(SHRINK_STEPS[1][2], "350k")
        self.assertEqual(evidence_check.SHRUNK_MIME, "video/mp4")


if __name__ == "__main__":
    unittest.main()
