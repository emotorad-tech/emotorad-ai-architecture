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


class FromEnvTests(unittest.TestCase):
    def test_no_bucket_means_no_store(self):
        self.assertIsNone(store_from_env(environ={}))

    def test_bucket_and_region_from_env(self):
        store = store_from_env(environ={BUCKET_ENV: "emotorad-ai-stage-media", "AWS_REGION": "ap-south-1"}, client=client())
        self.assertEqual(store.bucket, "emotorad-ai-stage-media")


if __name__ == "__main__":
    unittest.main()
