"""The S3 client, and the only file that imports boto3 for media.

Presigned URLs carry the customer's uploads: the browser talks to S3 directly
for an upload's bytes (the chat page sends videos this way), and this service
only signs the PUT. The exception is an inline photo on a message, which the
server writes itself (`put_bytes`) once it has decoded and checked it. A PUT
signature pins the content type
and length, so a client cannot presign a 2MB JPEG and then upload a 90MB file
under it. Reads are signed for fifteen minutes — long enough to load a
transcript, short enough that a leaked link is not a lasting one.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

BUCKET_ENV = "EMOTORAD_AI_MEDIA_BUCKET"
PUT_EXPIRY = 300
GET_EXPIRY = 900
# Seconds, and retries after the first try (botocore's `max_attempts`, so
# three tries in all). See S3Store.__init__.
CONNECT_TIMEOUT = 3
READ_TIMEOUT = 10
MAX_RETRIES = 2


class StorageError(Exception):
    """S3 answered with something other than success or not-found."""


class S3Store:
    def __init__(self, bucket: str, client: Any = None, region: Optional[str] = None) -> None:
        self.bucket = bucket
        if client is not None:
            self._client = client
        else:
            import boto3  # imported lazily: tests inject a client
            from botocore.config import Config

            region = region or os.environ.get("AWS_REGION", "ap-south-1")
            # SigV4 plus the regional endpoint: without both, botocore signs presigned
            # URLs against bucket.s3.amazonaws.com and S3 answers 307 for a bucket in
            # ap-south-1, which a browser PUT will not follow with its body.
            #
            # Short timeouts and two retries: an inline photo is put while the
            # customer waits for the reply, and botocore's defaults (60 s to
            # connect, 60 s to read) would hold a turn for minutes on a slow S3.
            # A put that gives up is logged and the photo stays inline for that
            # turn (api._inbound_attachments).
            self._client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url="https://s3.%s.amazonaws.com" % region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                    connect_timeout=CONNECT_TIMEOUT,
                    read_timeout=READ_TIMEOUT,
                    retries={"max_attempts": MAX_RETRIES, "mode": "standard"},
                ),
            )

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

    def delete_every_version(self, key: str) -> int:
        """Every version and delete marker of exactly `key`, gone for good:
        erasure, not a soft delete. Zero versions is a normal return of 0, not
        an error. Needs credentials allowed s3:ListBucketVersions and
        s3:DeleteObjectVersion; the instance role has neither, on purpose."""
        try:
            objects = self._versions_of(key)
            deleted = 0
            for start in range(0, len(objects), 1000):
                batch = objects[start:start + 1000]
                response = self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
                )
                errors = response.get("Errors") or []
                if errors:
                    raise StorageError(
                        "delete %r failed: %s" % (key, errors[0].get("Code") or "Error")
                    )
                deleted += len(batch)
            return deleted
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("delete %r failed: %s" % (key, type(exc).__name__)) from None

    def _versions_of(self, key: str) -> List[Dict[str, str]]:
        """Every version and delete marker whose Key is exactly `key`. Prefix
        also matches longer keys, so each entry is checked before it is kept."""
        objects: List[Dict[str, str]] = []
        kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": key}
        while True:
            response = self._client.list_object_versions(**kwargs)
            entries = list(response.get("Versions") or []) + list(response.get("DeleteMarkers") or [])
            for entry in entries:
                if entry.get("Key") == key:
                    objects.append({"Key": key, "VersionId": entry["VersionId"]})
            if not response.get("IsTruncated"):
                return objects
            kwargs["KeyMarker"] = response.get("NextKeyMarker")
            kwargs["VersionIdMarker"] = response.get("NextVersionIdMarker")


def store_from_env(environ: Optional[Mapping[str, str]] = None, client: Any = None) -> Optional[S3Store]:
    """None when no bucket is configured: uploads are then disabled and the
    playground keeps its local blobs. Never a silent default bucket."""
    env = environ if environ is not None else os.environ
    bucket = env.get(BUCKET_ENV, "").strip()
    if not bucket:
        return None
    return S3Store(bucket, client=client, region=env.get("AWS_REGION"))
