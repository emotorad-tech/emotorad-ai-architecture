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


if __name__ == "__main__":
    unittest.main()
