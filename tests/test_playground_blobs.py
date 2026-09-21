"""Playground attachments survive a redeploy on staging: bytes go to S3 under a
playground-only prefix, and are read back when the local cache is gone."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emotorad_ai import media, playground

PNG_B64 = base64.b64encode(b"\x89PNG fake").decode()


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = (data, mime)

    def get_bytes(self, key):
        return self.objects[key][0]


class _BrokenStore:
    """Every call raises, to exercise the failure paths in isolation."""

    def put_bytes(self, key, data, mime):
        raise RuntimeError("put boom")

    def get_bytes(self, key):
        raise RuntimeError("get boom")


class BlobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.blob_dir = Path(self.tmp.name)
        self.store = _Store()

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_a_store_blobs_stay_local(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=None):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
        self.assertNotIn("s3_key", records[0])
        self.assertTrue((self.blob_dir / (records[0]["blob_id"] + ".b64")).exists())

    def test_with_a_store_bytes_go_to_s3_under_the_playground_prefix(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=self.store):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
        key = records[0]["s3_key"]
        self.assertEqual(key, "customers/playground/chat-1/images/%s.png" % records[0]["blob_id"])
        self.assertEqual(self.store.objects[key], (b"\x89PNG fake", "image/png"))

    def test_a_missing_local_cache_is_refilled_from_s3(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=self.store):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
            (self.blob_dir / (records[0]["blob_id"] + ".b64")).unlink()
            self.assertEqual(playground._get_blob(records[0]), PNG_B64)
            self.assertTrue((self.blob_dir / (records[0]["blob_id"] + ".b64")).exists())

    def test_frames_missing_from_the_cache_are_re_derived_from_the_refilled_video(self):
        # After a redeploy the video bytes come back from S3 (via _get_blob),
        # but the per-frame blobs were only ever local, so `frame_blob_ids`
        # points at files that no longer exist. That must not read as "this
        # video could not be read" when the bytes are right there.
        video_blob_id = "video-blob"
        (self.blob_dir / (video_blob_id + ".b64")).write_text(PNG_B64)
        attachment = {
            "name": "a.mp4",
            "mime_type": "video/mp4",
            "blob_id": video_blob_id,
            "frame_blob_ids": ["missing"],
        }
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(playground, "_extract_frames", return_value=["ZmFrZQ=="]):
            blocks = playground._attachment_blocks([attachment])
        self.assertTrue(any(b.get("type") == "image" for b in blocks))
        self.assertFalse(any("could not be read" in b.get("text", "") for b in blocks))

    def test_a_put_failure_is_logged_with_the_key_not_swallowed(self):
        broken = _BrokenStore()
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=broken):
            with self.assertLogs(playground.__name__, level="WARNING") as log:
                records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
        self.assertNotIn("s3_key", records[0])
        self.assertTrue(any("put failed" in line and "customers/playground/chat-1" in line for line in log.output))
        # Never the bytes themselves, only the key and the exception type.
        self.assertFalse(any("PNG fake" in line for line in log.output))

    def test_a_get_failure_is_logged_with_the_key_not_swallowed(self):
        broken = _BrokenStore()
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=broken):
            attachment = {"blob_id": "missing-blob", "s3_key": "customers/playground/chat-1/images/missing-blob.png"}
            with self.assertLogs(playground.__name__, level="WARNING") as log:
                result = playground._get_blob(attachment)
        self.assertEqual(result, "")
        self.assertTrue(any("get failed" in line and "missing-blob.png" in line for line in log.output))


if __name__ == "__main__":
    unittest.main()
