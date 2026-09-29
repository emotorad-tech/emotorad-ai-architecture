"""A folder standing in for the media bucket, for end-to-end tests on a machine
with no AWS access (scripts/local_media_server.py).

The server takes it through the same interface it takes S3Store through, so a
photo from the chat page goes the whole way: stored under its customer key,
recorded in the `media` collection, and read back for the model.
"""

import importlib.util
import pathlib
import tempfile
import unittest

from emotorad_ai.storage.s3 import StorageError
from emotorad_ai.storage.uploads import UploadError

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = "customers/cl_ab12/c1/images/upl_0001.jpg"


def load_runner():
    spec = importlib.util.spec_from_file_location("local_media_server", ROOT / "scripts" / "local_media_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiskStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = load_runner().DiskStore(self.dir.name)

    def test_a_photo_is_kept_under_its_key_and_read_back(self):
        self.store.put_bytes(KEY, b"\xff\xd8jpeg", "image/jpeg")
        self.assertTrue((pathlib.Path(self.dir.name) / KEY).is_file())
        self.assertEqual(self.store.get_bytes(KEY), b"\xff\xd8jpeg")
        self.assertEqual(self.store.head(KEY), {"size": 6, "mime": "image/jpeg"})

    def test_the_bucket_name_says_it_is_not_s3(self):
        self.assertEqual(self.store.bucket, "local-folder")

    def test_a_missing_object_is_none_on_head_and_an_error_on_read(self):
        self.assertIsNone(self.store.head(KEY))
        with self.assertRaises(StorageError):
            self.store.get_bytes(KEY)

    def test_a_key_that_leaves_the_folder_is_refused(self):
        for key in ("../outside.jpg", "customers/../../x.jpg", "/etc/passwd", "C:/x.jpg"):
            with self.subTest(key=key), self.assertRaises(StorageError):
                self.store.put_bytes(key, b"x", "image/jpeg")

    def test_presigned_uploads_say_they_need_the_real_bucket(self):
        # post_upload turns an UploadError into its status: 503 makes the chat
        # page ask for a photo instead, as it does with no bucket at all.
        with self.assertRaises(UploadError) as caught:
            self.store.presign_put(KEY, "video/mp4", 10)
        self.assertEqual(caught.exception.status, 503)

    def test_erasure_removes_the_object(self):
        self.store.put_bytes(KEY, b"x", "image/jpeg")
        self.assertEqual(self.store.delete_every_version(KEY), 1)
        self.assertIsNone(self.store.head(KEY))
        self.assertEqual(self.store.delete_every_version(KEY), 0)


class RunnerTests(unittest.TestCase):
    def test_it_only_listens_on_this_machine(self):
        runner = load_runner()
        self.assertEqual(runner.HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
