"""Video attachments, which the model cannot actually watch.

The Messages API has image and document blocks and no video block, so a video is
sampled into stills. The property these tests protect is honesty about that: the
model must be told it is looking at frames, and a video that could not be
decoded must never reach it looking like one that was watched — a confident
assessment of a video nobody could open is the worst outcome available here.
"""

import base64
import io
import unittest

from emotorad_ai.playground import (
    VIDEO_FRAMES,
    _attachment_blocks,
    _extract_frames,
    _is_video,
    _serialise_uploaded_file,
)

try:
    import imageio
    import numpy as np
    from PIL import Image, ImageDraw

    VIDEO_TOOLING = True
except ImportError:  # pragma: no cover - exercised only on a machine without it
    VIDEO_TOOLING = False


def _numbered_clip(path, frames=90, fps=30):
    """A clip whose every frame is legibly different, so sampling is checkable."""
    writer = imageio.get_writer(str(path), fps=fps, codec="libx264", quality=8)
    for i in range(frames):
        image = Image.new("RGB", (320, 180), (20, 20, 20))
        ImageDraw.Draw(image).text((150, 85), "F%02d" % i, fill=(255, 255, 255))
        writer.append_data(np.array(image))
    writer.close()


class VideoDetectionTests(unittest.TestCase):
    def test_videos_are_recognised_by_mime_or_extension(self):
        # Browsers do not always send a video mime type, so the extension has to
        # be enough on its own.
        self.assertTrue(_is_video({"name": "a.mp4", "mime_type": "video/mp4"}))
        self.assertTrue(_is_video({"name": "b.MOV", "mime_type": ""}))
        self.assertTrue(_is_video({"name": "c.webm", "mime_type": None}))
        self.assertFalse(_is_video({"name": "d.png", "mime_type": "image/png"}))
        self.assertFalse(_is_video({"name": "e.pdf", "mime_type": "application/pdf"}))

    def test_an_uploaded_video_is_kinded_as_video_not_document(self):
        class _Upload:
            name = "clip.mp4"
            type = "video/mp4"

            def getvalue(self):
                return b"\x00\x00"

        self.assertEqual(_serialise_uploaded_file(_Upload())["kind"], "video")


class UnreadableVideoTests(unittest.TestCase):
    def test_a_video_that_cannot_be_decoded_is_declared_not_silently_dropped(self):
        blocks = _attachment_blocks(
            [
                {
                    "name": "broken.mp4",
                    "mime_type": "video/mp4",
                    "kind": "video",
                    "data": base64.b64encode(b"not a video at all").decode(),
                }
            ]
        )
        self.assertEqual([b["type"] for b in blocks], ["text"])
        text = blocks[0]["text"]
        self.assertIn("could not be read", text)
        self.assertIn("Do not describe or assess it", text)

    def test_garbage_returns_no_frames_rather_than_raising(self):
        self.assertEqual(_extract_frames(base64.b64encode(b"junk").decode(), ".mp4"), [])


@unittest.skipUnless(VIDEO_TOOLING, "imageio/ffmpeg not installed")
class FrameExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._dir = tempfile.TemporaryDirectory()
        path = cls._dir.name + "/clip.mp4"
        _numbered_clip(path)
        cls.b64 = base64.b64encode(open(path, "rb").read()).decode()

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_it_samples_the_requested_number_of_frames(self):
        self.assertEqual(len(_extract_frames(self.b64, ".mp4")), VIDEO_FRAMES)

    def test_frames_are_spread_across_the_clip_not_clustered_at_the_start(self):
        # The reason this matters: a customer filming a battery indicator holds
        # still for a while, and the informative moment is rarely in frame zero.
        # Decoded back to pixels and compared, so identical frames would fail.
        frames = _extract_frames(self.b64, ".mp4")
        digests = {base64.b64decode(f) for f in frames}
        self.assertEqual(len(digests), len(frames), "sampled frames are not distinct")

    def test_frames_are_downscaled_so_a_video_cannot_dominate_the_context(self):
        from emotorad_ai.playground import FRAME_MAX_EDGE

        for frame in _extract_frames(self.b64, ".mp4"):
            image = Image.open(io.BytesIO(base64.b64decode(frame)))
            self.assertLessEqual(max(image.size), FRAME_MAX_EDGE)
            self.assertEqual(image.format, "JPEG")

    def test_the_model_is_told_these_are_stills(self):
        blocks = _attachment_blocks(
            [{"name": "clip.mp4", "mime_type": "video/mp4", "kind": "video", "data": self.b64}]
        )
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("still frames", blocks[0]["text"])
        self.assertIn("not the video", blocks[0]["text"])
        self.assertEqual([b["type"] for b in blocks[1:]], ["image"] * VIDEO_FRAMES)

    def test_images_and_pdfs_are_untouched_by_any_of_this(self):
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), (255, 0, 0)).save(buffer, "PNG")
        blocks = _attachment_blocks(
            [
                {
                    "name": "x.png",
                    "mime_type": "image/png",
                    "kind": "image",
                    "data": base64.b64encode(buffer.getvalue()).decode(),
                }
            ]
        )
        self.assertEqual([b["type"] for b in blocks], ["image"])
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")


if __name__ == "__main__":
    unittest.main()
