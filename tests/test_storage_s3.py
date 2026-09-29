"""The one boto3 seam. Stubber verifies the exact request parameters, so a
presign that forgot to pin the content type cannot pass."""

import unittest
from unittest import mock

import boto3
from botocore.stub import Stubber

from emotorad_ai.storage.s3 import BUCKET_ENV, GET_EXPIRY, PUT_EXPIRY, S3Store, StorageError, store_from_env

KEY = "customers/clu_1/conv_1/images/upl_1.jpg"


def client():
    return boto3.client("s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y")


class PresignTests(unittest.TestCase):
    def test_put_pins_type_and_length_and_expiry(self):
        c = client()
        store = S3Store("bucket-x", client=c)
        with mock.patch.object(c, "generate_presigned_url", return_value="https://signed/put") as gen:
            out = store.presign_put(KEY, "image/jpeg", 1234)
        gen.assert_called_once_with(
            "put_object",
            Params={"Bucket": "bucket-x", "Key": KEY, "ContentType": "image/jpeg", "ContentLength": 1234},
            ExpiresIn=PUT_EXPIRY,
            HttpMethod="PUT",
        )
        self.assertEqual(out, {"url": "https://signed/put", "headers": {"Content-Type": "image/jpeg", "Content-Length": "1234"}, "expires_in": PUT_EXPIRY})

    def test_get_uses_the_read_expiry(self):
        c = client()
        store = S3Store("bucket-x", client=c)
        with mock.patch.object(c, "generate_presigned_url", return_value="https://signed/get") as gen:
            self.assertEqual(store.presign_get(KEY), "https://signed/get")
        gen.assert_called_once_with("get_object", Params={"Bucket": "bucket-x", "Key": KEY}, ExpiresIn=GET_EXPIRY)


class ObjectTests(unittest.TestCase):
    def test_head_returns_size_and_type(self):
        c = client()
        stub = Stubber(c)
        stub.add_response("head_object", {"ContentLength": 1234, "ContentType": "image/jpeg"}, {"Bucket": "b", "Key": KEY})
        stub.activate()
        self.assertEqual(S3Store("b", client=c).head(KEY), {"size": 1234, "mime": "image/jpeg"})

    def test_head_returns_none_for_a_missing_object(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404, expected_params={"Bucket": "b", "Key": KEY})
        stub.activate()
        self.assertIsNone(S3Store("b", client=c).head(KEY))

    def test_head_raises_on_any_other_failure(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403, expected_params={"Bucket": "b", "Key": KEY})
        stub.activate()
        with self.assertRaises(StorageError):
            S3Store("b", client=c).head(KEY)

    def test_get_and_put_bytes(self):
        import io

        c = client()
        stub = Stubber(c)
        stub.add_response("get_object", {"Body": io.BytesIO(b"abc")}, {"Bucket": "b", "Key": KEY})
        stub.add_response("put_object", {}, {"Bucket": "b", "Key": KEY, "Body": b"abc", "ContentType": "image/jpeg"})
        stub.activate()
        store = S3Store("b", client=c)
        self.assertEqual(store.get_bytes(KEY), b"abc")
        store.put_bytes(KEY, b"abc", "image/jpeg")
        stub.assert_no_pending_responses()


LONGER_KEY = KEY + "-thumb"


class DeleteEveryVersionTests(unittest.TestCase):
    def test_deletes_every_version_and_marker_across_pages_and_leaves_longer_key_alone(self):
        c = client()
        stub = Stubber(c)
        stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {"Key": KEY, "VersionId": "v1"},
                    {"Key": LONGER_KEY, "VersionId": "lv1"},
                ],
                "DeleteMarkers": [],
                "IsTruncated": True,
                "NextKeyMarker": KEY,
                "NextVersionIdMarker": "v1",
            },
            {"Bucket": "b", "Prefix": KEY},
        )
        stub.add_response(
            "list_object_versions",
            {
                "Versions": [{"Key": KEY, "VersionId": "v2"}],
                "DeleteMarkers": [{"Key": KEY, "VersionId": "dm1"}],
                "IsTruncated": False,
            },
            {"Bucket": "b", "Prefix": KEY, "KeyMarker": KEY, "VersionIdMarker": "v1"},
        )
        stub.add_response(
            "delete_objects",
            {},
            {
                "Bucket": "b",
                "Delete": {
                    "Objects": [
                        {"Key": KEY, "VersionId": "v1"},
                        {"Key": KEY, "VersionId": "v2"},
                        {"Key": KEY, "VersionId": "dm1"},
                    ],
                    "Quiet": True,
                },
            },
        )
        stub.activate()
        self.assertEqual(S3Store("b", client=c).delete_every_version(KEY), 3)
        stub.assert_no_pending_responses()

    def test_zero_versions_is_a_normal_zero_not_an_error(self):
        c = client()
        stub = Stubber(c)
        stub.add_response(
            "list_object_versions",
            {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
            {"Bucket": "b", "Prefix": KEY},
        )
        stub.activate()
        self.assertEqual(S3Store("b", client=c).delete_every_version(KEY), 0)
        stub.assert_no_pending_responses()

    def test_batches_deletes_at_1000_per_call(self):
        c = client()
        stub = Stubber(c)
        versions = [{"Key": KEY, "VersionId": "v%d" % i} for i in range(1500)]
        stub.add_response(
            "list_object_versions",
            {"Versions": versions, "DeleteMarkers": [], "IsTruncated": False},
            {"Bucket": "b", "Prefix": KEY},
        )
        stub.add_response(
            "delete_objects",
            {},
            {"Bucket": "b", "Delete": {"Objects": versions[:1000], "Quiet": True}},
        )
        stub.add_response(
            "delete_objects",
            {},
            {"Bucket": "b", "Delete": {"Objects": versions[1000:], "Quiet": True}},
        )
        stub.activate()
        self.assertEqual(S3Store("b", client=c).delete_every_version(KEY), 1500)
        stub.assert_no_pending_responses()

    def test_errors_in_the_delete_response_raise_storage_error(self):
        c = client()
        stub = Stubber(c)
        stub.add_response(
            "list_object_versions",
            {"Versions": [{"Key": KEY, "VersionId": "v1"}], "DeleteMarkers": [], "IsTruncated": False},
            {"Bucket": "b", "Prefix": KEY},
        )
        stub.add_response(
            "delete_objects",
            {"Errors": [{"Key": KEY, "VersionId": "v1", "Code": "AccessDenied", "Message": "no"}]},
            {"Bucket": "b", "Delete": {"Objects": [{"Key": KEY, "VersionId": "v1"}], "Quiet": True}},
        )
        stub.activate()
        with self.assertRaises(StorageError) as ctx:
            S3Store("b", client=c).delete_every_version(KEY)
        self.assertIn(KEY, str(ctx.exception))
        self.assertIn("AccessDenied", str(ctx.exception))

    def test_a_raising_client_gives_storage_error(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error(
            "list_object_versions", service_error_code="AccessDenied", http_status_code=403,
            expected_params={"Bucket": "b", "Prefix": KEY},
        )
        stub.activate()
        with self.assertRaises(StorageError) as ctx:
            S3Store("b", client=c).delete_every_version(KEY)
        self.assertIn(KEY, str(ctx.exception))


class RealClientTests(unittest.TestCase):
    def test_presign_get_uses_sigv4_and_the_regional_endpoint(self):
        # No injected client: this exercises the real boto3.client(...) construction
        # in S3Store.__init__, which is what actually produced 307s against the
        # global endpoint for the ap-south-1 bucket.
        with mock.patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}):
            store = S3Store("emotorad-ai-stage-media", region="ap-south-1")
            url = store.presign_get("k")
        self.assertIn("emotorad-ai-stage-media.s3.ap-south-1.amazonaws.com", url)
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)


class FromEnvTests(unittest.TestCase):
    def test_no_bucket_means_no_store(self):
        self.assertIsNone(store_from_env(environ={}))

    def test_bucket_and_region_from_env(self):
        store = store_from_env(environ={BUCKET_ENV: "emotorad-ai-stage-media", "AWS_REGION": "ap-south-1"}, client=client())
        self.assertEqual(store.bucket, "emotorad-ai-stage-media")


if __name__ == "__main__":
    unittest.main()
