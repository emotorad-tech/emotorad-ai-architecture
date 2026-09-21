"""The S3 client, and the only file that imports boto3 for media.

Presigned URLs are the whole design: the browser talks to S3 directly for the
bytes, and this service only ever signs. A PUT signature pins the content type
and length, so a client cannot presign a 2MB JPEG and then upload a 90MB file
under it. Reads are signed for fifteen minutes — long enough to load a
transcript, short enough that a leaked link is not a lasting one.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

BUCKET_ENV = "EMOTORAD_AI_MEDIA_BUCKET"
PUT_EXPIRY = 300
GET_EXPIRY = 900


class StorageError(Exception):
    """S3 answered with something other than success or not-found."""


class S3Store:
    def __init__(self, bucket: str, client: Any = None, region: Optional[str] = None) -> None:
        self.bucket = bucket
        if client is not None:
            self._client = client
        else:
            import boto3  # imported lazily: tests inject a client

            self._client = boto3.client("s3", region_name=region or os.environ.get("AWS_REGION", "ap-south-1"))

    def presign_put(self, key: str, mime: str, size: int) -> Dict[str, Any]:
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": mime, "ContentLength": size},
            ExpiresIn=PUT_EXPIRY,
            HttpMethod="PUT",
        )
        return {
            "url": url,
            "headers": {"Content-Type": mime, "Content-Length": str(size)},
            "expires_in": PUT_EXPIRY,
        }

    def presign_get(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=GET_EXPIRY
        )

    def head(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError("head %r failed: %s" % (key, code or type(exc).__name__)) from None
        return {"size": int(response["ContentLength"]), "mime": response.get("ContentType") or ""}

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise StorageError("get %r failed: %s" % (key, type(exc).__name__)) from None

    def put_bytes(self, key: str, data: bytes, mime: str) -> None:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)
        except Exception as exc:
            raise StorageError("put %r failed: %s" % (key, type(exc).__name__)) from None


def store_from_env(environ: Optional[Mapping[str, str]] = None, client: Any = None) -> Optional[S3Store]:
    """None when no bucket is configured: uploads are then disabled and the
    playground keeps its local blobs. Never a silent default bucket."""
    env = environ if environ is not None else os.environ
    bucket = env.get(BUCKET_ENV, "").strip()
    if not bucket:
        return None
    return S3Store(bucket, client=client, region=env.get("AWS_REGION"))
